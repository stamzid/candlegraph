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


def test_repo_tokens_file_loads_the_shipped_universe() -> None:
    """The shipped config is what docker compose *knows about*.

    It is no longer the same as what it ingests: since the `active` flag
    landed, the file lists every token whose history is stored and the
    worker polls only the active ones. The count grew by one when BNB was
    added for the active five; the assertion is a floor rather than an
    equality so adding a token is a config change, not a test change.
    """
    registry = load_tokens(REPO_ROOT / "config" / "tokens.yaml")

    assert len(registry) >= 10
    assert len(registry.active) == 5, "see the stage 0 exit criterion"
    assert set(registry.symbols) >= {"WETH", "WBTC", "USDC", "SHIB", "GNO", "BNB"}
    assert all(token.address.startswith("0x") for token in registry)
    assert all(token.name for token in registry)
    # Every shipped token carries an alias list for the knowledge layer.
    assert all(token.aliases for token in registry)


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


def test_aliases_are_loaded_and_stripped(tmp_path: Path) -> None:
    body = f'tokens:\n  - address: "{WBTC}"\n    symbol: WBTC\n    aliases: ["  Bitcoin ", "BTC"]\n'

    registry = load_tokens(write(tmp_path, body))

    assert registry.tokens[0].aliases == ("Bitcoin", "BTC")


def test_a_token_without_aliases_loads_with_an_empty_tuple(tmp_path: Path) -> None:
    body = f'tokens:\n  - address: "{WBTC}"\n    symbol: WBTC\n'

    assert load_tokens(write(tmp_path, body)).tokens[0].aliases == ()


@pytest.mark.parametrize(
    ("aliases", "message"),
    [
        ("aliases: not-a-list", "aliases must be a list"),
        ('aliases: ["ok", 7]', r"aliases\[1\] must be a non-empty string"),
        ('aliases: [" "]', r"aliases\[0\] must be a non-empty string"),
    ],
)
def test_malformed_aliases_are_rejected(tmp_path: Path, aliases: str, message: str) -> None:
    body = f'tokens:\n  - address: "{WBTC}"\n    symbol: WBTC\n    {aliases}\n'

    with pytest.raises(TokenConfigError, match=message):
        load_tokens(write(tmp_path, body))


def test_an_alias_shared_by_two_tokens_is_rejected(tmp_path: Path) -> None:
    """A shared alias would make a document's mention ambiguous; case-insensitive."""
    body = (
        f'tokens:\n  - address: "{WBTC}"\n    symbol: WBTC\n    aliases: [Bitcoin]\n'
        f'  - address: "{SHIB}"\n    symbol: SHIB\n    aliases: [bitcoin]\n'
    )

    with pytest.raises(TokenConfigError, match="duplicate alias"):
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


# --- the active flag (stage 0 closeout) --------------------------------------

ACTIVE_YAML = """
tokens:
  - address: "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    symbol: WETH
    active: true
  - address: "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
    symbol: WBTC
  - address: "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    symbol: USDC
    active: false
"""


def test_a_token_without_the_flag_is_active(tmp_path: Path) -> None:
    """A file written before the flag existed must be unchanged by it."""
    registry = load_tokens(write(tmp_path, ACTIVE_YAML))

    assert registry.by_symbol("WBTC") is not None
    assert registry.by_symbol("WBTC").active is True  # type: ignore[union-attr]


def test_ingestion_polls_only_the_active_tokens(tmp_path: Path) -> None:
    registry = load_tokens(write(tmp_path, ACTIVE_YAML))

    assert registry.active_symbols == ("WETH", "WBTC")
    assert registry.inactive_symbols == ("USDC",)
    assert len(registry.active_addresses) == 2


def test_deactivating_a_token_does_not_hide_its_stored_history(tmp_path: Path) -> None:
    """`active: false` means stop *fetching*, never forget.

    Invariant 4 keeps the rows, so every lookup, the analytics layer and
    the knowledge layer must still see the token. Removing the entry
    instead would orphan its history, which is why a retired token stays
    listed.
    """
    registry = load_tokens(write(tmp_path, ACTIVE_YAML))

    assert registry.by_symbol("USDC") is not None, "still addressable"
    assert "USDC" in registry.symbols, "still in the full list"
    assert "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48" in registry.addresses
    assert "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48" not in registry.active_addresses


def test_a_non_boolean_flag_is_refused(tmp_path: Path) -> None:
    """YAML turns `no` into False but `"false"` into a truthy string, so a
    typo would silently keep a token in the poll."""
    bad = ACTIVE_YAML.replace("active: false", 'active: "false"')

    with pytest.raises(TokenConfigError, match="active must be true or false"):
        load_tokens(write(tmp_path, bad))


def test_deactivating_every_token_is_refused(tmp_path: Path) -> None:
    """An ingest run that fetches nothing is almost certainly not meant."""
    none_active = """
tokens:
  - address: "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    symbol: WETH
    active: false
"""

    with pytest.raises(TokenConfigError, match="every token is marked inactive"):
        load_tokens(write(tmp_path, none_active))


def test_the_shipped_config_tracks_exactly_the_five_the_roadmap_names() -> None:
    """Stage 0's exit criterion: the stack ingests exactly five tokens
    while `tokens.yaml` still lists the full set.

    GNO and SHIB are there *because* their history is gapped — they are
    the fixtures the gap-filling work is judged on.
    """
    registry = load_tokens(REPO_ROOT / "config" / "tokens.yaml")

    assert set(registry.active_symbols) == {"WBTC", "WETH", "BNB", "GNO", "SHIB"}
    assert len(registry.tokens) > 5, "the retired ones stay listed, or their history is orphaned"
    assert set(registry.inactive_symbols) == {"USDC", "USDT", "DAI", "LINK", "UNI", "AAVE"}
