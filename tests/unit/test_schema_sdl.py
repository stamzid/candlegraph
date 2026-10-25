"""The committed GraphQL SDL must match the live schema.

`schema.graphql` is the contract clients read. Regenerating it is one command
(`make sdl`), so there is no excuse for it drifting; this test is what makes
forgetting impossible.
"""

from __future__ import annotations

import pytest

from candlegraph.schema import chart_schema
from candlegraph.settings import REPO_ROOT

SDL_PATH = REPO_ROOT / "schema.graphql"


def test_the_committed_sdl_is_current() -> None:
    if not SDL_PATH.is_file():
        pytest.fail(f"{SDL_PATH.name} is missing; run `make sdl`")

    assert SDL_PATH.read_text(encoding="utf-8").strip() == str(chart_schema).strip(), (
        "schema.graphql is stale; run `make sdl` and commit the result"
    )
