"""The explanation fields of the GraphQL API, through the schema with a fake database.

What is pinned: reads are free and never touch the network or write a row;
the mutation writes a row and does not search unless asked; bad input is a
GraphQL error, not a 500.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from candlegraph.api.explanations import (
    fetch_signal_explanation,
    fetch_signal_explanations,
    run_explain_signal,
)
from candlegraph.api.schema import chart_schema
from candlegraph.storage import set_db
from tests.conftest import FakeDatabase, make_settings

BAR = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 4, 9, 30, tzinfo=UTC)


def stored_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "strategy": "bollinger",
        "symbol": "WBTC",
        "period_start_unix": int(BAR.timestamp()),
        "position": -1,
        "numeric_context": {"score": 2.31, "move_24h": 0.08},
        "citations": [
            {
                "n": 1,
                "chunk_id": 5,
                "document_id": 9,
                "url": "https://news.example/unlock",
                "title": "Unlock",
                "published_at": "2026-10-24T09:12:00+00:00",
                "retrieved_at": "2026-10-24T14:03:00+00:00",
                "quote": "WBTC moved on an unlock.",
                "legs": "both",
                "score": 0.03,
            }
        ],
        "rationale": "WBTC rose on an unlock [1].",
        "model": None,
        "generated_at": NOW,
    }
    base.update(overrides)
    return base


def price_rows(symbol: str, n: int = 60) -> list[dict[str, Any]]:
    start = BAR - timedelta(hours=n - 1)
    rows = []
    for i in range(n):
        price = 100.0 + (i if i > n - 5 else 0)
        moment = start + timedelta(hours=i)
        rows.append(
            {
                "symbol": symbol,
                "token_id": f"0x{symbol}",
                "period_start_unix": int(moment.timestamp()),
                "timestamp": moment,
                "open": Decimal(str(price)),
                "high": Decimal(str(price)),
                "low": Decimal(str(price)),
                "close": Decimal(str(price)),
                "price_usd": Decimal(str(price)),
                "volume_usd": Decimal("1"),
                "total_value_locked_usd": Decimal("1"),
            }
        )
    return rows


def hybrid_rows() -> list[dict[str, Any]]:
    return [
        {
            "id": 5,
            "document_id": 9,
            "ordinal": 0,
            "text": "WBTC moved on an unlock.",
            "url": "https://news.example/unlock",
            "title": "Unlock",
            "published_at": datetime(2026, 10, 24, 9, 12, tzinfo=UTC),
            "retrieved_at": datetime(2026, 10, 24, 14, 3, tzinfo=UTC),
            "score": 1 / 61,
            "dense_rank": 1,
            "lexical_rank": None,
        }
    ]


@pytest.fixture
def api_db(tokens_file: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    """Prices for WBTC and SHIB, one stored explanation, a corpus to cite, a fake embedder, no keys."""
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr(
        "candlegraph.api.explanations.get_settings",
        lambda: make_settings(
            tokens_file=str(tokens_file),
            embedding_provider="fake",
            knowledge_reference_symbol="SHIB",
            tavily_api_key="tvly-x",
        ),
    )
    db = FakeDatabase(
        get_latest_explanation=[stored_row()],
        get_explanation=[stored_row(position=1)],
        get_recent_explanations=[
            {
                "strategy": "bollinger",
                "symbol": "WBTC",
                "period_start_unix": int(BAR.timestamp()),
                "position": -1,
                "citations": 1,
                "model": None,
                "generated_at": NOW,
            }
        ],
        get_price_history=price_rows("WBTC") + price_rows("SHIB"),
        hybrid_search=hybrid_rows(),
        lexical_search=hybrid_rows(),
        count_embedding_backlog=[{"missing": 0, "stale": 0, "total": 0}],
    )
    set_db(db)
    return db


# --- reads ---------------------------------------------------------------------


def test_the_stored_explanation_is_served_read_only(api_db: FakeDatabase) -> None:
    found = fetch_signal_explanation("wbtc")

    assert found is not None
    assert found.symbol == "WBTC" and found.position == -1 and found.position_label == "short"
    assert found.bar == "2026-10-24T14:00:00+00:00" and found.score == 2.31
    assert found.citations[0].url == "https://news.example/unlock"
    assert found.citations[0].published_at == "2026-10-24T09:12:00+00:00"
    assert found.news_status == "stored"
    assert api_db.executed == [] and api_db.upserts == [], "a read writes nothing"
    assert [label for label, _ in api_db.fetches] == ["get_latest_explanation"]


def test_a_bar_can_be_named_and_nothing_stored_is_null(api_db: FakeDatabase) -> None:
    exact = fetch_signal_explanation("WBTC", at=BAR.isoformat())
    assert exact is not None and exact.position == 1
    assert api_db.fetches[-1][1]["period_start_unix"] == int(BAR.timestamp())

    api_db.reads["get_latest_explanation"] = []
    assert fetch_signal_explanation("WBTC", "crossover") is None


def test_the_summaries_list_the_newest_explanations(api_db: FakeDatabase) -> None:
    rows = fetch_signal_explanations("wbtc", limit=5)

    assert len(rows) == 1 and rows[0].citations == 1 and rows[0].symbol == "WBTC"
    assert api_db.fetches[-1][1] == {"symbol": "WBTC", "limit": 5}
    with pytest.raises(ValueError, match="limit"):
        fetch_signal_explanations(limit=0)


def test_the_read_queries_work_through_the_schema(api_db: FakeDatabase) -> None:
    result = chart_schema.execute_sync(
        """
        {
          signalExplanation(tokenSymbol: "WBTC") {
            symbol positionLabel rationale newsStatus numericContext
            citations { n url publishedAt legs }
          }
          signalExplanations(tokenSymbol: "WBTC", limit: 3) { strategy citations }
        }
        """
    )

    assert result.errors is None, result.errors
    assert result.data is not None
    found = result.data["signalExplanation"]
    assert found["positionLabel"] == "short" and found["newsStatus"] == "stored"
    assert found["numericContext"]["score"] == 2.31
    assert found["citations"][0]["publishedAt"] == "2026-10-24T09:12:00+00:00"
    assert result.data["signalExplanations"] == [{"strategy": "bollinger", "citations": 1}]


# --- the mutation --------------------------------------------------------------------


def test_the_mutation_explains_the_latest_bar_stores_it_and_does_not_search_by_default(
    api_db: FakeDatabase,
) -> None:
    result = chart_schema.execute_sync(
        """
        mutation {
          explainSignal(tokenSymbol: "WBTC", strategy: "bollinger") {
            symbol bar positionLabel newsStatus denseUsed rejectedCitations
            citations { n url }
            numericContext
          }
        }
        """
    )

    assert result.errors is None, result.errors
    assert result.data is not None
    data = result.data["explainSignal"]
    assert data["symbol"] == "WBTC" and data["bar"] == BAR.isoformat()
    assert data["newsStatus"] == "skipped", "a key is configured, and still no credit without search: true"
    assert data["denseUsed"] is True and data["citations"][0]["url"] == "https://news.example/unlock"
    assert data["numericContext"]["reference"]["symbol"] == "SHIB"
    assert data["rejectedCitations"] == []
    [(_, params)] = [(name, p) for name, p in api_db.executed if name == "upsert_explanation"]
    assert params is not None and params["symbol"] == "WBTC"
    assert json.loads(params["citations"])[0]["url"] == "https://news.example/unlock"
    assert all(label != "insert_source_call" for label, _ in api_db.executed), (
        "no ledger row: nothing was called"
    )


def test_the_mutation_rejects_bad_input_as_a_graphql_error_not_a_crash(api_db: FakeDatabase) -> None:
    unknown = chart_schema.execute_sync('mutation { explainSignal(tokenSymbol: "DOGE") { symbol } }')
    assert unknown.errors and "unknown token" in unknown.errors[0].message

    strategy = chart_schema.execute_sync(
        'mutation { explainSignal(tokenSymbol: "WBTC", strategy: "nope") { symbol } }'
    )
    assert strategy.errors and "unknown strategy" in strategy.errors[0].message

    when = chart_schema.execute_sync(
        'mutation { explainSignal(tokenSymbol: "WBTC", at: "yesterday") { symbol } }'
    )
    assert when.errors and "ISO 8601" in when.errors[0].message

    missing = chart_schema.execute_sync(
        'mutation { explainSignal(tokenSymbol: "WBTC", at: "2031-01-01T00:00:00+00:00") { symbol } }'
    )
    assert missing.errors and "no bollinger bar" in missing.errors[0].message


def test_run_explain_signal_can_pick_a_bar(api_db: FakeDatabase) -> None:
    earlier = BAR - timedelta(hours=3)

    data = run_explain_signal("WBTC", at=earlier.isoformat())

    assert data.bar == earlier.isoformat() and data.period_start_unix == int(earlier.timestamp())


def test_the_published_sdl_keeps_the_explanation_field_names() -> None:
    sdl = str(chart_schema)

    for fragment in (
        'signalExplanation(tokenSymbol: String!, strategy: String! = "bollinger", at: String = null): SignalExplanation',
        "signalExplanations(tokenSymbol: String = null, limit: Int! = 20): [ExplanationSummary!]!",
        'explainSignal(tokenSymbol: String!, strategy: String! = "bollinger", at: String = null, '
        "search: Boolean! = false, days: Int = null): SignalExplanation!",
        "numericContext: JSON!",
        "citations: [Citation!]!",
        "rejectedCitations: [Int!]!",
    ):
        assert fragment in sdl, fragment
