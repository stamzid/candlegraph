"""The migration preflight, and the configuration it exists to protect.

These pin the 2026-10-28 outage: a volume initialised by plain ``postgres:17``
and a version table stamped with a revision that was renamed before commit.
Neither condition had a test, so neither had a message naming its cause.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from alembic.config import Config
from alembic.script import ScriptDirectory

from candlegraph.settings import REPO_ROOT
from candlegraph.storage.preflight import (
    explain_missing_preload,
    explain_unknown_revision,
    preload_libraries,
    timescaledb_is_preloaded,
    unknown_revisions,
)

# --- the preload rule --------------------------------------------------------


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("timescaledb", True),
        ("'timescaledb'", True),
        ("pg_stat_statements, timescaledb", True),
        ("$libdir/timescaledb", True),
        ("timescaledb.so", True),
        ("", False),
        ("''", False),
        ("pg_stat_statements", False),
        # The prefix is not the library: a lookalike must not pass.
        ("timescaledb_toolkit", False),
    ],
)
def test_the_preload_check_reads_the_setting_the_way_postgres_writes_it(setting: str, expected: bool) -> None:
    assert timescaledb_is_preloaded(setting) is expected


def test_preload_libraries_normalises_paths_quotes_and_whitespace() -> None:
    assert preload_libraries(" '$libdir/timescaledb.so' , pg_stat_statements ") == {
        "timescaledb",
        "pg_stat_statements",
    }


def test_an_empty_preload_setting_is_explained_not_assumed() -> None:
    """A volume from plain postgres:17 has the library on disk but not preloaded.

    `pg_available_extensions` says available, so the old check passed and the
    migration went on to a FATAL. The message has to name the real cause.
    """
    message = explain_missing_preload("")

    assert "(empty)" in message
    assert "initialised by a plain postgres image" in message
    assert "shared_preload_libraries=timescaledb" in message


# --- the unknown-revision rule -----------------------------------------------


def test_a_revision_this_checkout_lacks_is_reported_by_name() -> None:
    assert unknown_revisions(["d2f8b6a41c35"], ["4a4f4da52183", "e4b9c7a05f13"]) == ["d2f8b6a41c35"]
    assert unknown_revisions(["e4b9c7a05f13"], ["4a4f4da52183", "e4b9c7a05f13"]) == []
    assert unknown_revisions([], ["4a4f4da52183"]) == [], "a fresh database has no revision yet"


def test_the_unknown_revision_message_names_both_causes() -> None:
    """The run-stack skill used to attribute this only to a sibling worktree.

    The outage was the other cause: a draft revision applied locally and then
    renamed before commit. Both call for different actions, so both appear.
    """
    message = explain_unknown_revision(["d2f8b6a41c35"], ["a", "b", "c"])

    assert "d2f8b6a41c35" in message
    assert "3 known revisions" in message
    assert "COMPOSE_PROJECT_NAME" in message
    assert "renamed or rewritten before it was committed" in message
    assert "must never change once it has been applied" in message


# --- the configuration those rules protect -----------------------------------


def alembic_scripts() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))


def test_the_migration_chain_is_linear_with_one_head() -> None:
    """Every committed revision reaches the single head by `down_revision`.

    A second head, or a revision that chains to an id that is not in the
    tree, is how a database ends up stamped with something no checkout can
    reach. Alembic would only report that at upgrade time, on a stack.
    """
    scripts = alembic_scripts()
    revisions = list(scripts.walk_revisions())
    known = {script.revision for script in revisions}

    assert scripts.get_heads() == [scripts.get_current_head()], "exactly one head"
    for script in revisions:
        parents = script.down_revision
        if parents is None:
            continue
        for parent in (parents,) if isinstance(parents, str) else parents:
            assert parent in known, f"{script.revision} revises {parent}, which is not in alembic/versions"


def test_every_revision_file_is_named_after_its_id() -> None:
    """Renaming a revision id without renaming the file, or the reverse, is the
    drift that left a database stamped with an id no file carried."""
    for script in alembic_scripts().walk_revisions():
        assert Path(script.path).name.startswith(f"{script.revision}_"), script.path


def compose_file() -> dict[str, object]:
    with (REPO_ROOT / "docker-compose.yaml").open(encoding="utf-8") as handle:
        return dict(yaml.safe_load(handle))


def test_compose_forces_the_timescale_preload_on_every_volume() -> None:
    """The image sets the preload only on initdb, so an older volume comes up
    without it. The service, not the volume's history, has to decide this."""
    services = compose_file()["services"]
    assert isinstance(services, dict)
    postgres = services["postgres"]

    assert postgres["image"] == "timescale/timescaledb:2.31.0-pg17"
    command = " ".join(postgres["command"])
    assert "shared_preload_libraries=timescaledb" in command


def test_the_database_image_is_pinned_identically_everywhere() -> None:
    """Compose, CI and the testcontainer must agree, or a migration proven in
    one place says nothing about the others."""
    from tests.integration.conftest import POSTGRES_IMAGE

    services = compose_file()["services"]
    assert isinstance(services, dict)
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert services["postgres"]["image"] == POSTGRES_IMAGE
    assert f"image: {POSTGRES_IMAGE}" in ci
