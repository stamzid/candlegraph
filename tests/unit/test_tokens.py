"""The tokens file is validated on load, so bad config fails loudly and early."""

from __future__ import annotations

from pathlib import Path

import pytest

from candlegraph.settings import REPO_ROOT
from candlegraph.tokens import (
    Token,
    TokenConfigError,
    TokenRegistry,
    get_token_registry,
    load_tokens,
    reset_token_cache,
)
from tests.conftest import SHIB, WBTC


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "tokens.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_repo_tokens_file_loads_and_holds_three_tokens() -> None:
    """The shipped config is what docker compose ingests."""
    registry = load_tokens(REPO_ROOT / "config" / "tokens.yaml")

    assert registry.symbols == ("WBTC", "SHIB", "GNO")
    assert len(registry) == 3
    assert all(token.address.startswith("0x") for token in registry)


def test_loads_addresses_symbols_and_names(registry: TokenRegistry) -> None:
    assert registry.tokens[0] == Token(address=WBTC, symbol="WBTC", name="Wrapped BTC")
    assert registry.addresses == (WBTC, SHIB)


def test_symbol_by_address_maps_every_token(registry: TokenRegistry) -> None:
    assert registry.symbol_by_address == {WBTC: "WBTC", SHIB: "SHIB"}


def test_lookup_by_symbol_is_case_insensitive(registry: TokenRegistry) -> None:
    assert registry.by_symbol("wbtc") == registry.by_symbol("WBTC")
    assert registry.by_symbol("WBTC") is not None


def test_lookup_of_an_unknown_symbol_returns_none(registry: TokenRegistry) -> None:
    assert registry.by_symbol("NOPE") is None


def test_lookup_by_address_normalises_case(registry: TokenRegistry) -> None:
    found = registry.by_address(WBTC.upper())
    assert found is not None
    assert found.symbol == "WBTC"


def test_addresses_are_lowercased_on_load(tmp_path: Path) -> None:
    """The subgraph returns lowercase ids; config must match for joins to work."""
    path = write(tmp_path, f'tokens:\n  - address: "{WBTC.upper()}"\n    symbol: WBTC\n')

    assert load_tokens(path).addresses == (WBTC,)


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(TokenConfigError, match="cannot read tokens file"):
        load_tokens(tmp_path / "absent.yaml")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("just a string\n", "top-level 'tokens' key"),
        ("other_key: []\n", "top-level 'tokens' key"),
        ("tokens: []\n", "non-empty list"),
        ("tokens:\n  - just-a-string\n", "must be a mapping"),
        ("tokens:\n  - symbol: WBTC\n", "string 'address' and 'symbol'"),
        ('tokens:\n  - address: "0xdeadbeef"\n    symbol: X\n', "not a 0x-prefixed"),
        (f'tokens:\n  - address: "{WBTC}"\n    symbol: " "\n', "symbol must not be empty"),
        (f'tokens:\n  - address: "{WBTC}"\n    symbol: X\n    name: 7\n', "name must be a string"),
    ],
)
def test_malformed_config_is_rejected(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(TokenConfigError, match=message):
        load_tokens(write(tmp_path, body))


def test_duplicate_address_is_rejected(tmp_path: Path) -> None:
    body = f'tokens:\n  - address: "{WBTC}"\n    symbol: A\n  - address: "{WBTC}"\n    symbol: B\n'

    with pytest.raises(TokenConfigError, match="duplicate address"):
        load_tokens(write(tmp_path, body))


def test_duplicate_symbol_is_rejected(tmp_path: Path) -> None:
    """Two tokens sharing a symbol would make the API's lookup ambiguous."""
    body = f'tokens:\n  - address: "{WBTC}"\n    symbol: DUP\n  - address: "{SHIB}"\n    symbol: dup\n'

    with pytest.raises(TokenConfigError, match="duplicate symbol"):
        load_tokens(write(tmp_path, body))


def test_registry_is_immutable(registry: TokenRegistry) -> None:
    with pytest.raises(AttributeError):
        registry.tokens = ()  # type: ignore[misc]


def test_get_token_registry_follows_the_configured_path(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    reset_token_cache()

    assert get_token_registry().symbols == ("WBTC", "SHIB")
    assert get_token_registry() is get_token_registry()
