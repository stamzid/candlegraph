"""The registry -> asset transform is pure, and the sync is one upsert."""

from __future__ import annotations

import json

from candlegraph.knowledge.assets import asset_rows, default_aliases, sync_assets
from candlegraph.tokens import Token, TokenRegistry
from tests.conftest import SHIB, WBTC, FakeDatabase


def registry(*tokens: Token) -> TokenRegistry:
    return TokenRegistry(tokens=tokens)


def test_a_token_with_aliases_keeps_exactly_those_aliases() -> None:
    token = Token(address=WBTC, symbol="WBTC", name="Wrapped BTC", aliases=("WBTC", "Bitcoin"))

    rows = asset_rows(registry(token))

    assert rows == [
        {"id": WBTC, "symbol": "WBTC", "kind": "token", "aliases": json.dumps(["WBTC", "Bitcoin"])}
    ]


def test_a_token_without_aliases_defaults_to_symbol_and_name() -> None:
    token = Token(address=SHIB, symbol="SHIB", name="SHIBA INU")

    rows = asset_rows(registry(token))

    assert json.loads(rows[0]["aliases"]) == ["SHIB", "SHIBA INU"]


def test_the_default_set_does_not_repeat_a_name_equal_to_the_symbol() -> None:
    token = Token(address=SHIB, symbol="SHIB", name="shib")

    assert default_aliases(token) == ("SHIB",)


def test_a_token_without_a_name_defaults_to_the_symbol_alone() -> None:
    token = Token(address=SHIB, symbol="SHIB")

    assert default_aliases(token) == ("SHIB",)


def test_sync_assets_issues_one_upsert_with_every_row() -> None:
    db = FakeDatabase()
    tokens = (
        Token(address=WBTC, symbol="WBTC", name="Wrapped BTC", aliases=("WBTC", "Bitcoin")),
        Token(address=SHIB, symbol="SHIB", name="SHIBA INU"),
    )

    written = sync_assets(db, registry(*tokens))  # type: ignore[arg-type]

    assert written == 2
    assert len(db.upserts) == 1
    label, rows = db.upserts[0]
    assert label == "upsert_asset"
    assert [row["id"] for row in rows] == [WBTC, SHIB]
