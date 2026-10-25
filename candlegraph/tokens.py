"""The set of tokens candlegraph tracks, loaded from a YAML file.

Keeping the list in data rather than code means adding a token is a config
change, and lets tests build a registry without touching the repo's own file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from candlegraph.settings import get_settings

ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")


class TokenConfigError(ValueError):
    """The tokens file is missing, malformed, or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class Token:
    """A single tracked ERC-20."""

    address: str
    symbol: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class TokenRegistry:
    """An immutable, indexed view over the configured tokens."""

    tokens: tuple[Token, ...]

    @property
    def addresses(self) -> tuple[str, ...]:
        return tuple(token.address for token in self.tokens)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(token.symbol for token in self.tokens)

    @property
    def symbol_by_address(self) -> dict[str, str]:
        """Address -> symbol, the shape the ingest path annotates rows with."""
        return {token.address: token.symbol for token in self.tokens}

    def by_address(self, address: str) -> Token | None:
        wanted = address.lower()
        return next((t for t in self.tokens if t.address == wanted), None)

    def by_symbol(self, symbol: str) -> Token | None:
        """Look up by symbol, case-insensitively."""
        wanted = symbol.casefold()
        return next((t for t in self.tokens if t.symbol.casefold() == wanted), None)

    def __len__(self) -> int:
        return len(self.tokens)

    def __iter__(self) -> Any:
        return iter(self.tokens)


def _parse_entry(index: int, raw: Any) -> Token:
    if not isinstance(raw, dict):
        raise TokenConfigError(f"tokens[{index}] must be a mapping, got {type(raw).__name__}")

    address = raw.get("address")
    symbol = raw.get("symbol")
    if not isinstance(address, str) or not isinstance(symbol, str):
        raise TokenConfigError(f"tokens[{index}] needs string 'address' and 'symbol' keys")

    address = address.strip().lower()
    symbol = symbol.strip()
    if not ADDRESS_RE.match(address):
        raise TokenConfigError(
            f"tokens[{index}] address {address!r} is not a 0x-prefixed 20-byte hex address"
        )
    if not symbol:
        raise TokenConfigError(f"tokens[{index}] symbol must not be empty")

    name = raw.get("name")
    if name is not None and not isinstance(name, str):
        raise TokenConfigError(f"tokens[{index}] name must be a string when present")

    return Token(address=address, symbol=symbol, name=name)


def load_tokens(path: Path) -> TokenRegistry:
    """Read and validate a tokens YAML file.

    Raises:
        TokenConfigError: the file is absent, is not a token list, holds a
            malformed address, or repeats an address or symbol.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TokenConfigError(f"cannot read tokens file {path}: {exc}") from exc

    document = yaml.safe_load(text)
    if not isinstance(document, dict) or "tokens" not in document:
        raise TokenConfigError(f"{path} must be a mapping with a top-level 'tokens' key")

    entries = document["tokens"]
    if not isinstance(entries, list) or not entries:
        raise TokenConfigError(f"{path}: 'tokens' must be a non-empty list")

    tokens = tuple(_parse_entry(i, raw) for i, raw in enumerate(entries))

    _reject_duplicates(path, "address", [t.address for t in tokens])
    _reject_duplicates(path, "symbol", [t.symbol.casefold() for t in tokens])

    return TokenRegistry(tokens=tokens)


def _reject_duplicates(path: Path, field: str, values: list[str]) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise TokenConfigError(f"{path}: duplicate {field} {value!r}")
        seen.add(value)


@lru_cache(maxsize=1)
def get_token_registry() -> TokenRegistry:
    """The registry for the configured ``TOKENS_FILE``, loaded on first use."""
    return load_tokens(get_settings().tokens_path)


def reset_token_cache() -> None:
    """Drop the cached registry. Used by tests that point at another file."""
    get_token_registry.cache_clear()
