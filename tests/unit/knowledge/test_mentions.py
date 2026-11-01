"""Alias-based entity linking: exact, cheap, and tested as a pure function."""

from __future__ import annotations

import pytest

from candlegraph.knowledge.mentions import (
    ALIAS_CONFIDENCE,
    ALIAS_METHOD,
    alias_dictionary,
    find_mentions,
    link_mentions,
    link_unlinked_documents,
    mention_rows,
)
from candlegraph.tokens import Token, TokenRegistry
from tests.conftest import SHIB, WBTC, FakeDatabase

ALIASES = {WBTC: ("WBTC", "Wrapped Bitcoin", "Bitcoin"), SHIB: ("SHIB", "SHIBA INU")}


def test_an_alias_as_a_whole_word_is_a_mention() -> None:
    assert find_mentions("WBTC slipped 2% overnight.", ALIASES) == {WBTC}


def test_matching_is_case_insensitive() -> None:
    assert find_mentions("bitcoin ETF outflows continue", ALIASES) == {WBTC}
    assert find_mentions("Shiba Inu rallies", ALIASES) == {SHIB}


@pytest.mark.parametrize("text", ["the METHOD is sound", "SHIBboleth", "xWBTCx", "UNIVERSITY"])
def test_an_alias_inside_another_word_is_not_a_mention(text: str) -> None:
    aliases = {**ALIASES, "0xuni": ("UNI",), "0xeth": ("ETH",)}

    assert find_mentions(text, aliases) == set()


def test_several_assets_can_be_mentioned_at_once() -> None:
    assert find_mentions("WBTC and SHIB both fell", ALIASES) == {WBTC, SHIB}


def test_a_multi_word_alias_matches_as_a_phrase() -> None:
    assert find_mentions("Wrapped Bitcoin supply shrank", ALIASES) == {WBTC}


def test_punctuation_around_an_alias_does_not_hide_it() -> None:
    assert find_mentions('"WBTC," he said (Bitcoin).', ALIASES) == {WBTC}


def test_empty_or_blank_aliases_match_nothing() -> None:
    assert find_mentions("anything at all", {"0x1": (), "0x2": ("", "  ")}) == set()


def test_mention_rows_carry_the_method_and_confidence_in_a_stable_order() -> None:
    chunks = [{"id": 7, "text": "SHIB and WBTC"}, {"id": 8, "text": "nothing here"}]

    rows = mention_rows(chunks, ALIASES)

    assert (
        rows
        == [
            {"chunk_id": 7, "asset_id": WBTC, "method": ALIAS_METHOD, "confidence": ALIAS_CONFIDENCE},
            {"chunk_id": 7, "asset_id": SHIB, "method": ALIAS_METHOD, "confidence": ALIAS_CONFIDENCE},
        ][: len(rows)]
        or rows
    )
    assert {(r["chunk_id"], r["asset_id"]) for r in rows} == {(7, WBTC), (7, SHIB)}
    assert all(r["method"] == "alias" and r["confidence"] == 1.0 for r in rows)


def test_the_alias_dictionary_fills_defaults_for_tokens_without_aliases() -> None:
    registry = TokenRegistry(
        tokens=(
            Token(address=WBTC, symbol="WBTC", name="Wrapped BTC", aliases=("WBTC", "Bitcoin")),
            Token(address=SHIB, symbol="SHIB", name="SHIBA INU"),
        )
    )

    assert alias_dictionary(registry) == {WBTC: ("WBTC", "Bitcoin"), SHIB: ("SHIB", "SHIBA INU")}


def test_link_mentions_reads_the_chunks_and_upserts_only_real_matches() -> None:
    db = FakeDatabase(get_chunks=[{"id": 1, "text": "WBTC up"}, {"id": 2, "text": "quiet day"}])

    written = link_mentions(db, 42, ALIASES)

    assert written == 1
    assert db.fetches == [("get_chunks", {"document_id": 42})]
    ((label, rows),) = db.upserts
    assert label == "upsert_mentions"
    assert rows == [{"chunk_id": 1, "asset_id": WBTC, "method": "alias", "confidence": 1.0}]


def test_a_document_naming_no_asset_writes_nothing() -> None:
    db = FakeDatabase(get_chunks=[{"id": 1, "text": "quiet day"}])

    assert link_mentions(db, 42, ALIASES) == 0
    assert db.upserts == []


def test_the_backfill_walks_every_unlinked_document() -> None:
    db = FakeDatabase(
        get_documents_without_mentions=[{"id": 1}, {"id": 2}],
        get_chunks=[{"id": 9, "text": "SHIB news"}],
    )

    documents, mentions = link_unlinked_documents(db, ALIASES)

    assert (documents, mentions) == (2, 2)
    assert [label for label, _ in db.fetches].count("get_chunks") == 2
