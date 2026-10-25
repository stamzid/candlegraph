"""Ingest orchestration: cursors, per-token isolation, and the pruning switch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from candlegraph.ingest import (
    IngestResult,
    ingest_once,
    ingest_token_hours,
    ingest_token_metadata,
    lookback_start,
    resolve_cursors,
)
from candlegraph.subgraph_client import SubgraphError, SubgraphPage
from candlegraph.tokens import TokenRegistry
from tests.conftest import SHIB, WBTC, FakeDatabase, make_settings


class FakeSubgraphClient:
    """Replays canned pages per token, or raises a canned error."""

    def __init__(
        self,
        pages: dict[str, list[list[dict[str, Any]]]] | None = None,
        tokens: list[dict[str, Any]] | None = None,
        fail: dict[str, str] | None = None,
    ) -> None:
        self.pages = pages or {}
        self.tokens = tokens if tokens is not None else []
        self.fail = fail or {}
        self.cursors_seen: list[tuple[str, int]] = []
        self.tokens_requested: list[list[str]] = []

    def fetch_tokens(self, token_ids: list[str]) -> list[dict[str, Any]]:
        self.tokens_requested.append(list(token_ids))
        if "__metadata__" in self.fail:
            raise SubgraphError(self.fail["__metadata__"])
        return self.tokens

    def iter_token_hour_datas(self, token_id: str, since: int) -> Any:
        self.cursors_seen.append((token_id, since))
        if token_id in self.fail:
            raise SubgraphError(self.fail[token_id])
        for rows in self.pages.get(token_id, []):
            last = max(int(row["periodStartUnix"]) for row in rows)
            yield SubgraphPage(rows=rows, last_timestamp=last)


def hour_rows(token: str, hours: list[int]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"{token}-{hour}",
            "periodStartUnix": hour * 3600,
            "open": "1",
            "high": "3",
            "low": "0",
            "close": "2",
            "priceUSD": "2",
        }
        for hour in hours
    ]


# --- lookback_start --------------------------------------------------------


def test_lookback_start_subtracts_whole_days(frozen_now: datetime) -> None:
    assert lookback_start(7, frozen_now) == int((frozen_now - timedelta(days=7)).timestamp())


def test_lookback_start_defaults_to_now() -> None:
    before = int(datetime.now(tz=UTC).timestamp())

    value = lookback_start(1)

    assert before - 86400 - 5 <= value <= before - 86400 + 5


# --- resolve_cursors -------------------------------------------------------


def test_cursors_fall_back_to_the_lookback_window(registry: TokenRegistry, frozen_now: datetime) -> None:
    db = FakeDatabase()
    expected = lookback_start(7, frozen_now)

    assert resolve_cursors(db, registry, 7, frozen_now) == {WBTC: expected, SHIB: expected}


def test_cursors_resume_from_the_newest_stored_hour(registry: TokenRegistry, frozen_now: datetime) -> None:
    """A restart must not re-fetch a window it already has."""
    db = FakeDatabase(get_latest_timestamps=[{"token_id": WBTC, "latest_unix": 1_700_000_000}])

    cursors = resolve_cursors(db, registry, 7, frozen_now)

    assert cursors[WBTC] == 1_700_000_000
    assert cursors[SHIB] == lookback_start(7, frozen_now)


def test_cursors_cover_exactly_the_registry(registry: TokenRegistry, frozen_now: datetime) -> None:
    """Stored rows for a token that has since been removed are not re-fetched."""
    db = FakeDatabase(get_latest_timestamps=[{"token_id": "0x" + "cd" * 20, "latest_unix": 123}])

    assert set(resolve_cursors(db, registry, 7, frozen_now)) == set(registry.addresses)


# --- metadata --------------------------------------------------------------


def test_metadata_upserts_every_returned_row(registry: TokenRegistry) -> None:
    db = FakeDatabase()
    client = FakeSubgraphClient(tokens=[{"id": WBTC}, {"id": SHIB}])

    assert ingest_token_metadata(db, client, registry) == 2
    assert client.tokens_requested == [[WBTC, SHIB]]
    assert db.upserts == [("insert_token", [{"id": WBTC}, {"id": SHIB}])]


def test_metadata_writes_nothing_when_the_subgraph_returns_none(registry: TokenRegistry) -> None:
    db = FakeDatabase()

    assert ingest_token_metadata(db, FakeSubgraphClient(tokens=[]), registry) == 0
    assert db.upserts == []


# --- hourly rows -----------------------------------------------------------


def test_hours_are_written_page_by_page(registry: TokenRegistry) -> None:
    db = FakeDatabase()
    client = FakeSubgraphClient(pages={WBTC: [hour_rows(WBTC, [1, 2]), hour_rows(WBTC, [3])]})

    assert ingest_token_hours(db, client, registry, WBTC, 0) == 3
    assert [label for label, _ in db.upserts] == ["insert_token_hours", "insert_token_hours"]


def test_hours_are_annotated_with_the_configured_symbol(registry: TokenRegistry) -> None:
    db = FakeDatabase()
    client = FakeSubgraphClient(pages={WBTC: [hour_rows(WBTC, [1])]})

    ingest_token_hours(db, client, registry, WBTC, 0)

    [(_, written)] = db.upserts
    assert written[0]["symbol"] == "WBTC"
    assert written[0]["id"] == WBTC
    assert written[0]["timestamp"] == datetime(1970, 1, 1, 1, tzinfo=UTC)


def test_a_page_of_only_unknown_tokens_writes_nothing(registry: TokenRegistry) -> None:
    db = FakeDatabase()
    client = FakeSubgraphClient(pages={WBTC: [hour_rows("0x" + "ef" * 20, [1])]})

    assert ingest_token_hours(db, client, registry, WBTC, 0) == 0
    assert db.upserts == []


def test_the_cursor_is_passed_through_to_the_client(registry: TokenRegistry) -> None:
    client = FakeSubgraphClient()

    ingest_token_hours(FakeDatabase(), client, registry, WBTC, 999)

    assert client.cursors_seen == [(WBTC, 999)]


# --- ingest_once -----------------------------------------------------------


def test_a_full_run_writes_metadata_and_hours(registry: TokenRegistry, frozen_now: datetime) -> None:
    db = FakeDatabase()
    client = FakeSubgraphClient(
        tokens=[{"id": WBTC}, {"id": SHIB}],
        pages={WBTC: [hour_rows(WBTC, [1, 2])], SHIB: [hour_rows(SHIB, [1])]},
    )

    result = ingest_once(db, client, registry, make_settings(), frozen_now)

    assert result.ok
    assert result.tokens_written == 2
    assert result.hours_written == 3
    assert result.hours_by_symbol == {"WBTC": 2, "SHIB": 1}


def test_one_failing_token_does_not_stop_the_others(registry: TokenRegistry, frozen_now: datetime) -> None:
    db = FakeDatabase()
    client = FakeSubgraphClient(pages={SHIB: [hour_rows(SHIB, [1])]}, fail={WBTC: "gateway exploded"})

    result = ingest_once(db, client, registry, make_settings(), frozen_now)

    assert not result.ok
    assert result.errors == {"WBTC": "gateway exploded"}
    assert result.hours_by_symbol == {"SHIB": 1}
    assert result.hours_written == 1


def test_a_metadata_failure_is_recorded_but_hours_still_ingest(
    registry: TokenRegistry, frozen_now: datetime
) -> None:
    db = FakeDatabase()
    client = FakeSubgraphClient(
        pages={WBTC: [hour_rows(WBTC, [1])]}, fail={"__metadata__": "no tokens query"}
    )

    result = ingest_once(db, client, registry, make_settings(), frozen_now)

    assert result.errors == {"__metadata__": "no tokens query"}
    assert result.hours_written == 1


def test_history_is_kept_by_default(registry: TokenRegistry, frozen_now: datetime) -> None:
    """Deleting history on every poll is the opposite of what analytics needs."""
    db = FakeDatabase()

    result = ingest_once(db, FakeSubgraphClient(), registry, make_settings(), frozen_now)

    assert db.executed == []
    assert result.rows_pruned == 0


def test_pruning_happens_only_when_retain_history_is_off(
    registry: TokenRegistry, frozen_now: datetime
) -> None:
    db = FakeDatabase()
    db.delete_rowcount = 17
    settings = make_settings(retain_history=False, lookback_days=3)

    result = ingest_once(db, FakeSubgraphClient(), registry, settings, frozen_now)

    assert db.executed == [("delete_older_data", {"interval_start": lookback_start(3, frozen_now)})]
    assert result.rows_pruned == 17


def test_an_empty_run_is_still_a_success(registry: TokenRegistry, frozen_now: datetime) -> None:
    result = ingest_once(FakeDatabase(), FakeSubgraphClient(), registry, make_settings(), frozen_now)

    assert result.ok
    assert result.hours_written == 0


@pytest.mark.parametrize(
    ("errors", "expected"),
    [({}, True), ({"WBTC": "x"}, False)],
)
def test_result_ok_reflects_errors(errors: dict[str, str], expected: bool) -> None:
    assert IngestResult(errors=errors).ok is expected


def test_run_ingest_builds_and_closes_a_client(
    registry: TokenRegistry, frozen_now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HTTP client is per-run, so a run cannot leak connections."""
    from candlegraph import ingest as ingest_module

    closed: list[bool] = []

    class RecordingClient(FakeSubgraphClient):
        def __init__(self, _settings: Any) -> None:
            super().__init__(tokens=[{"id": WBTC}])

        def __enter__(self) -> RecordingClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            closed.append(True)

    monkeypatch.setattr(ingest_module, "SubgraphClient", RecordingClient)

    result = ingest_module.run_ingest(FakeDatabase(), registry, make_settings(), frozen_now)

    assert result.tokens_written == 1
    assert closed == [True]
