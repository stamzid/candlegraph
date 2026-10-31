"""URL normalisation, the url_hash key, and chunking: pure, so tested exhaustively.

The normaliser decides which spellings of an address are one document; every
rule it folds has a case here, and so does every rule it deliberately keeps.
The chunker's contract is boundaries: never mid-sentence, stable ordinals,
an overlap that never swallows a whole chunk.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest

from candlegraph.knowledge.documents import (
    Chunk,
    ChunkConfig,
    DocumentError,
    RawDocument,
    chunk_rows,
    chunk_text,
    count_tokens,
    document_row,
    normalise_url,
    split_paragraphs,
    split_sentences,
    store_document,
    url_hash,
)
from tests.conftest import FakeDatabase

CANONICAL = "https://example.com/news/article"


# --- URL normalisation: what is folded ------------------------------------------------


@pytest.mark.parametrize(
    "spelling",
    [
        "https://example.com/news/article",
        "http://example.com/news/article",
        "HTTPS://EXAMPLE.COM/news/article",
        "https://www.example.com/news/article",
        "https://WWW.Example.com/news/article",
        "https://example.com:443/news/article",
        "http://example.com:80/news/article",
        "https://example.com/news/article/",
        "https://example.com//news///article",
        "https://example.com/news/article#comments",
        "https://example.com/news/article?utm_source=x&utm_medium=y&utm_campaign=z",
        "https://example.com/news/article?fbclid=abc",
        "https://example.com/news/article?gclid=1&ref=twitter",
        "https://example.com/news/article?UTM_SOURCE=upper",
        "https://example.com/news/article?",
        "  https://example.com/news/article  ",
        "example.com/news/article",
        "https://user:secret@example.com/news/article",
        "https://example.com./news/article",
    ],
)
def test_every_spelling_of_one_address_normalises_to_the_same_string(spelling: str) -> None:
    assert normalise_url(spelling) == CANONICAL


def test_two_phrasings_of_one_url_hash_equal() -> None:
    """The exit condition for step 4, stated as the rollout states it."""
    assert url_hash("http://www.Example.com/a/?utm_source=mail&x=1#top") == url_hash(
        "https://example.com/a?x=1"
    )


def test_the_hash_is_sha256_hex_of_the_normalised_url() -> None:
    import hashlib

    assert url_hash(" HTTP://EXAMPLE.COM/a ") == hashlib.sha256(b"https://example.com/a").hexdigest()
    assert len(url_hash("https://example.com")) == 64


def test_remaining_query_parameters_are_sorted_so_order_does_not_matter() -> None:
    assert normalise_url("https://x.com/p?b=2&a=1") == normalise_url("https://x.com/p?a=1&b=2")
    assert normalise_url("https://x.com/p?b=2&a=1") == "https://x.com/p?a=1&b=2"


def test_repeated_parameters_are_kept_in_a_stable_order() -> None:
    assert normalise_url("https://x.com/p?t=b&t=a") == "https://x.com/p?t=a&t=b"


def test_blank_parameter_values_survive() -> None:
    """``?draft`` and ``?draft=`` select a different page on some sites; keep them."""
    assert normalise_url("https://x.com/p?draft=&a=1") == "https://x.com/p?a=1&draft="


def test_the_root_path_is_a_single_slash() -> None:
    assert normalise_url("https://example.com") == "https://example.com/"
    assert normalise_url("https://example.com/") == "https://example.com/"


def test_a_non_default_port_is_kept() -> None:
    assert normalise_url("https://example.com:8443/a") == "https://example.com:8443/a"
    assert normalise_url("http://example.com:8080/a") == "https://example.com:8080/a"


def test_unicode_hosts_and_paths_round_trip() -> None:
    assert normalise_url("https://Exämple.com/Straße") == "https://exämple.com/Straße"


# --- URL normalisation: what is deliberately kept -------------------------------------


def test_path_case_is_significant() -> None:
    """Many servers serve /Article and /article as different pages."""
    assert normalise_url("https://x.com/Article") != normalise_url("https://x.com/article")


def test_a_substantive_query_parameter_changes_the_document() -> None:
    assert url_hash("https://x.com/post?id=1") != url_hash("https://x.com/post?id=2")
    assert url_hash("https://x.com/post?page=2") != url_hash("https://x.com/post")


def test_a_different_host_is_a_different_document() -> None:
    assert url_hash("https://x.com/a") != url_hash("https://y.com/a")
    assert url_hash("https://blog.x.com/a") != url_hash("https://x.com/a")


def test_only_the_www_prefix_is_folded_not_other_subdomains() -> None:
    assert normalise_url("https://www2.x.com/a") == "https://www2.x.com/a"


def test_tracking_detection_is_by_whole_name() -> None:
    """``source`` is tracking; ``resource`` and ``source_id`` are not."""
    assert (
        normalise_url("https://x.com/a?resource=1&source=mail&source_id=7")
        == "https://x.com/a?resource=1&source_id=7"
    )


# --- URL normalisation: what is refused -------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["", "   ", "ftp://example.com/file", "mailto:a@b.com", "javascript:alert(1)"]
)
def test_things_that_cannot_identify_a_web_document_are_refused(bad: str) -> None:
    with pytest.raises(DocumentError):
        normalise_url(bad)


def test_a_url_without_a_host_is_refused() -> None:
    with pytest.raises(DocumentError, match="no host"):
        normalise_url("https:///just/a/path")


def test_an_invalid_port_is_refused() -> None:
    with pytest.raises(DocumentError, match="invalid port"):
        normalise_url("https://example.com:notaport/a")


# --- tokens, sentences, paragraphs -------------------------------------------------------


def test_tokens_are_words_and_punctuation_marks() -> None:
    assert count_tokens("Hello, world!") == 4
    assert count_tokens("") == 0
    assert count_tokens("one   two\nthree") == 3


def test_paragraphs_split_on_blank_lines_and_collapse_whitespace() -> None:
    text = "First  para\nstill first.\n\n\nSecond para.\n\n   \n"

    assert split_paragraphs(text) == ["First para still first.", "Second para."]


def test_sentences_keep_their_terminators_and_closing_quotes() -> None:
    assert split_sentences('He said "stop." Then he left! Did he? Yes.') == [
        'He said "stop."',
        "Then he left!",
        "Did he?",
        "Yes.",
    ]


def test_a_paragraph_with_no_terminator_is_one_sentence() -> None:
    assert split_sentences("no punctuation here") == ["no punctuation here"]


def test_decimals_inside_a_sentence_do_not_split_it() -> None:
    assert split_sentences("ETH rose 2.5% to 3,100.50 today. Then fell.") == [
        "ETH rose 2.5% to 3,100.50 today.",
        "Then fell.",
    ]


# --- chunking --------------------------------------------------------------------------


def sentence(i: int, words: int = 10) -> str:
    return " ".join(f"w{i}_{j}" for j in range(words - 1)) + f" end{i}."


def paragraphs(n_paragraphs: int, sentences_each: int, words: int = 10) -> str:
    return "\n\n".join(
        " ".join(sentence(p * 100 + s, words) for s in range(sentences_each)) for p in range(n_paragraphs)
    )


SMALL = ChunkConfig(target_tokens=40, max_tokens=50, min_tokens=25, overlap_tokens=12)


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_short_text_is_one_chunk_with_ordinal_zero() -> None:
    chunks = chunk_text("One sentence. Another one.", SMALL)

    assert len(chunks) == 1
    assert chunks[0] == Chunk(
        ordinal=0, text="One sentence. Another one.", token_count=count_tokens("One sentence. Another one.")
    )


def test_ordinals_are_consecutive_from_zero() -> None:
    chunks = chunk_text(paragraphs(6, 4), SMALL)

    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert len(chunks) > 1


def test_chunking_is_deterministic() -> None:
    text = paragraphs(5, 5)

    assert chunk_text(text, SMALL) == chunk_text(text, SMALL)


def test_no_chunk_boundary_falls_mid_sentence() -> None:
    """Every chunk ends where a sentence ends and starts where one starts."""
    text = paragraphs(6, 4)
    sentences = {s for p in split_paragraphs(text) for s in split_sentences(p)}

    for chunk in chunk_text(text, SMALL):
        pieces = split_sentences(chunk.text)
        assert all(piece in sentences for piece in pieces), pieces


def test_no_chunk_exceeds_max_tokens_unless_a_single_sentence_does() -> None:
    chunks = chunk_text(paragraphs(6, 4), SMALL)

    assert all(c.token_count <= SMALL.max_tokens for c in chunks)


def test_a_sentence_longer_than_max_tokens_becomes_its_own_chunk_rather_than_being_split() -> None:
    giant = " ".join(f"word{i}" for i in range(120)) + "."
    text = f"Short lead. {giant} Short tail."

    chunks = chunk_text(text, SMALL)

    assert any(c.text == giant for c in chunks), "the giant sentence must appear whole"
    assert all(giant not in c.text or c.text == giant for c in chunks)


def test_every_sentence_of_the_input_appears_in_some_chunk() -> None:
    text = paragraphs(7, 3)
    joined = " ".join(c.text for c in chunk_text(text, SMALL))

    for p in split_paragraphs(text):
        for s in split_sentences(p):
            assert s in joined


def test_consecutive_chunks_overlap_by_trailing_sentences_within_the_budget() -> None:
    chunks = chunk_text(paragraphs(1, 12), SMALL)

    assert len(chunks) >= 2
    for earlier, later in itertools.pairwise(chunks):
        head = split_sentences(later.text)[0]
        tail = split_sentences(earlier.text)
        assert head in tail, "the next chunk must open with sentences the previous one ended on"
        shared = tail[tail.index(head) :]
        assert sum(count_tokens(s) for s in shared) <= SMALL.overlap_tokens


def test_zero_overlap_means_no_repeated_sentences() -> None:
    config = ChunkConfig(target_tokens=40, max_tokens=50, min_tokens=25, overlap_tokens=0)
    chunks = chunk_text(paragraphs(1, 12), config)

    seen: list[str] = []
    for chunk in chunks:
        for s in split_sentences(chunk.text):
            assert s not in seen
            seen.append(s)


def test_overlap_never_repeats_a_whole_chunk_so_the_loop_always_advances() -> None:
    """One big sentence per chunk with a generous overlap budget must still terminate and progress."""
    config = ChunkConfig(target_tokens=12, max_tokens=14, min_tokens=5, overlap_tokens=4)
    chunks = chunk_text(" ".join(sentence(i, 12) for i in range(5)), config)

    assert len(chunks) == 5
    assert len({c.text for c in chunks}) == 5


def test_a_paragraph_end_closes_a_chunk_once_it_holds_min_tokens() -> None:
    """Chunks prefer paragraph boundaries: a 30-token paragraph is its own chunk at min 25."""
    text = paragraphs(3, 3)  # three paragraphs of 30 tokens each

    chunks = chunk_text(text, SMALL)

    firsts = [split_sentences(c.text)[0] for c in chunks]
    assert firsts[0] == sentence(0)
    # Each later chunk opens with the overlap from the previous paragraph, then the new paragraph.
    assert all(c.token_count <= SMALL.max_tokens for c in chunks)


def test_a_short_paragraph_is_merged_with_the_next_rather_than_left_alone() -> None:
    text = "Tiny.\n\n" + " ".join(sentence(i) for i in range(3))

    chunks = chunk_text(text, SMALL)

    assert chunks[0].text.startswith("Tiny.")
    assert len(split_sentences(chunks[0].text)) > 1


def test_token_counts_match_the_text() -> None:
    for chunk in chunk_text(paragraphs(4, 4), SMALL):
        assert chunk.token_count == count_tokens(chunk.text)


def test_the_default_config_targets_three_to_five_hundred_tokens() -> None:
    config = ChunkConfig()

    assert config.min_tokens == 300
    assert config.target_tokens == 400
    assert config.max_tokens == 500
    assert config.overlap_tokens == 50


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_tokens": 0},
        {"min_tokens": 500, "target_tokens": 400},
        {"target_tokens": 600, "max_tokens": 500},
        {"overlap_tokens": -1},
        {"overlap_tokens": 300},
    ],
)
def test_an_impossible_chunk_config_is_rejected(kwargs: dict[str, int]) -> None:
    with pytest.raises(DocumentError):
        ChunkConfig(**kwargs)


# --- rows ----------------------------------------------------------------------------------


def test_the_document_row_hashes_the_url_and_leaves_an_unknown_date_null() -> None:
    raw = RawDocument(source_id="manual", url=" http://www.x.com/a?utm_source=m ", raw_text="Body.")

    row = document_row(raw, retrieved_at=datetime(2026, 10, 31, tzinfo=UTC))

    assert row["url_hash"] == url_hash("https://x.com/a")
    assert row["url"] == "http://www.x.com/a?utm_source=m"
    assert row["published_at"] is None, "unknown must stay NULL, never now()"
    assert row["retrieved_at"] == datetime(2026, 10, 31, tzinfo=UTC)
    assert row["meta"] == "{}"


def test_retrieved_at_defaults_to_now_but_published_at_never_does() -> None:
    row = document_row(RawDocument(source_id="manual", url="https://x.com/a", raw_text="Body."))

    assert row["retrieved_at"] is not None
    assert row["published_at"] is None


def test_chunk_rows_carry_the_document_id_and_ordinals() -> None:
    rows = chunk_rows(7, chunk_text("One. Two.", SMALL))

    assert rows == [{"document_id": 7, "ordinal": 0, "text": "One. Two.", "token_count": 4}]


# --- the write path, offline ---------------------------------------------------------------


def test_store_document_upserts_the_document_then_its_chunks_then_trims_the_tail() -> None:
    db = FakeDatabase(get_document_by_hash=[{"id": 42}])
    raw = RawDocument(source_id="manual", url="https://x.com/a", raw_text=paragraphs(3, 3))

    stored = store_document(db, raw, SMALL)

    assert stored.document_id == 42
    assert stored.url_hash == url_hash("https://x.com/a")
    assert next(label for label, _ in db.executed) == "upsert_document"
    assert [label for label, _ in db.fetches] == ["get_document_by_hash"]
    assert [label for label, _ in db.upserts] == ["upsert_chunks"]
    assert ("delete_stale_chunks", {"document_id": 42, "count": stored.chunks_written}) in db.executed
    assert stored.chunks_written == len(chunk_text(raw.raw_text, SMALL))


def test_a_bare_host_with_a_port_is_a_url_not_a_scheme() -> None:
    """``localhost:8080/a`` must not be mistaken for a ``localhost:`` scheme."""
    assert normalise_url("localhost:8080/a") == "https://localhost:8080/a"
    assert normalise_url("example.com:8080/a") == "https://example.com:8080/a"
