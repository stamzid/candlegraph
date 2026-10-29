"""Checks that run before ``alembic upgrade head``, with errors that say what to do.

Two faults took the stack down on 2026-10-28 and neither produced a message
that named its cause (see ``docs/incidents/2026-10-28-stack-would-not-start/``):

* the database volume had been initialised by plain ``postgres:17``, so the
  TimescaleDB image reused it without ``shared_preload_libraries`` and
  ``CREATE EXTENSION timescaledb`` killed the backend with a ``FATAL``;
* the volume's ``alembic_version`` named a draft revision that had been
  renamed before it was committed, so Alembic could not place it in the chain.

Everything here is pure: the caller reads the server's settings and the
version table and passes the values in, so the rules are unit-tested without
a database and the migrations only wire them up.
"""

from __future__ import annotations

from collections.abc import Collection

TIMESCALE_LIBRARY = "timescaledb"


def preload_libraries(setting: str) -> set[str]:
    """Parse a ``shared_preload_libraries`` value into bare library names.

    PostgreSQL accepts a comma-separated list, optionally quoted, where each
    entry may carry a ``$libdir/`` prefix or a ``.so`` suffix. ``''`` and
    whitespace mean nothing is preloaded.
    """
    names: set[str] = set()
    for raw in setting.split(","):
        entry = raw.strip().strip("'\"").strip()
        if not entry:
            continue
        entry = entry.rsplit("/", 1)[-1]
        if entry.endswith(".so"):
            entry = entry[: -len(".so")]
        names.add(entry)
    return names


def timescaledb_is_preloaded(setting: str) -> bool:
    """Whether ``CREATE EXTENSION timescaledb`` can succeed on this server.

    The extension insists on being in ``shared_preload_libraries``; without
    it the statement is a backend ``FATAL``, not an error a migration can
    catch, and ``pg_available_extensions`` still lists it as available.
    """
    return TIMESCALE_LIBRARY in preload_libraries(setting)


def explain_missing_preload(setting: str) -> str:
    """The error to raise when the server has the extension but is not preloading it."""
    shown = setting if setting.strip() else "(empty)"
    return (
        "the timescaledb extension is installed on this server but not preloaded: "
        f"shared_preload_libraries is {shown}. CREATE EXTENSION would abort the "
        "connection, so the migration stops here. This almost always means the data "
        "directory was initialised by a plain postgres image before the stack moved "
        "to timescale/timescaledb, so the image's init script never ran. The compose "
        "file passes -c shared_preload_libraries=timescaledb for exactly this case; "
        "if you are not on compose, add it to postgresql.conf and restart the server. "
        "See docs/storage/timescaledb/DESIGN.md."
    )


def unknown_revisions(current: Collection[str], known: Collection[str]) -> list[str]:
    """The revisions the database claims to be at that this checkout has never heard of."""
    known_set = set(known)
    return sorted(revision for revision in current if revision not in known_set)


def explain_unknown_revision(unknown: Collection[str], known: Collection[str]) -> str:
    """The error to raise when ``alembic_version`` names a revision this tree lacks.

    Alembic's own message (``Can't locate revision identified by ...``) is
    accurate but gives no cause, and the two causes call for different
    actions, so both are spelled out.
    """
    missing = ", ".join(sorted(unknown))
    return (
        f"the database is stamped at revision {missing}, which does not exist in this "
        f"checkout ({len(known)} known revisions under alembic/versions). Two things "
        "produce this. (1) Another worktree whose directory shares this one's basename "
        "is using the same Compose project and therefore the same volume: run yours "
        "under an explicit COMPOSE_PROJECT_NAME with distinct host ports instead of "
        "`make down`, which would delete their data. (2) A draft migration was applied "
        "to this database and then renamed or rewritten before it was committed, so no "
        "committed revision can reach the schema that is actually there: the volume "
        "has to be rebuilt (`make down && make up`), and a revision id must never "
        "change once it has been applied anywhere. See "
        "docs/incidents/2026-10-28-stack-would-not-start/POSTMORTEM.md."
    )
