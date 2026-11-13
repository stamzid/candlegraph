"""Ingest orchestration: cursors, per-token isolation, and the pruning switch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from candlegraph.ingestion.runner import (
    IngestResult,
    ingest_once,
    ingest_token_hours,
    ingest_token_metadata,
    lookback_start,
    resolve_cursors,
)
from candlegraph.ingestion.subgraph import SubgraphError, SubgraphPage
from candlegraph.tokens import TokenRegistry
from tests.conftest import SHIB, WBTC, FakeDatabase, make_settings


def token_settings(**kw: Any) -> Any:
    """Settings with pool ingestion off: most runner tests are token-only."""
    return make_settings(ingest_pools=False, **kw)


class FakeSubgraphClient:
    """Replays canned pages per token, or raises a canned error."""

    def __init__(
        self,
        pages: dict[str, list[list[dict[str, Any]]]] | None = None,
        tokens: list[dict[str, Any]] | None = None,
        fail: dict[str, str] | None = None,
        pools: list[dict[str, Any]] | None = None,
        pool_pages: dict[str, list[list[dict[str, Any]]]] | None = None,
    ) -> None:
        self.pages = pages or {}
        self.tokens = tokens if tokens is not None else []
        self.fail = fail or {}
        self.pools = pools or []
        self.pool_pages = pool_pages or {}
        self.cursors_seen: list[tuple[str, int]] = []
        self.tokens_requested: list[list[str]] = []
        self.pools_requested: list[tuple[list[str], int]] = []

    def fetch_pools_for_tokens(self, token_ids: list[str], first: int) -> list[dict[str, Any]]:
        self.pools_requested.append((list(token_ids), first))
        if "__pools__" in self.fail:
            raise SubgraphError(self.fail["__pools__"])
        return self.pools

    def iter_pool_hour_datas(self, pool_id: str, since: int) -> Any:
        self.cursors_seen.append((pool_id, since))
        if pool_id in self.fail:
            raise SubgraphError(self.fail[pool_id])
        for rows in self.pool_pages.get(pool_id, []):
            last = max(int(row["periodStartUnix"]) for row in rows)
            yield SubgraphPage(rows=rows, last_timestamp=last)

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


def test_metadata_upserts_every_returned_row(registry: TokenRegistry, frozen_now: datetime) -> None:
    db = FakeDatabase()
    client = FakeSubgraphClient(tokens=[{"id": WBTC}, {"id": SHIB}])

    assert ingest_token_metadata(db, client, registry, frozen_now) == 2
    assert client.tokens_requested == [[WBTC, SHIB]]
    [(label, rows)] = db.upserts
    assert label == "insert_token"
    assert [row["id"] for row in rows] == [WBTC, SHIB]
    # Absent fields become None, never 0: "not collected" is not "zero".
    assert rows[0]["volumeUSD"] is None
    assert rows[0]["updated_at"] == frozen_now


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

    result = ingest_once(db, client, registry, token_settings(), frozen_now)

    assert result.ok
    assert result.tokens_written == 2
    assert result.hours_written == 3
    assert result.hours_by_symbol == {"WBTC": 2, "SHIB": 1}


def test_one_failing_token_does_not_stop_the_others(registry: TokenRegistry, frozen_now: datetime) -> None:
    db = FakeDatabase()
    client = FakeSubgraphClient(pages={SHIB: [hour_rows(SHIB, [1])]}, fail={WBTC: "gateway exploded"})

    result = ingest_once(db, client, registry, token_settings(), frozen_now)

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

    result = ingest_once(db, client, registry, token_settings(), frozen_now)

    assert result.errors == {"__metadata__": "no tokens query"}
    assert result.hours_written == 1


def test_a_run_never_deletes_anything(registry: TokenRegistry, frozen_now: datetime) -> None:
    """Stored history is the input to analytics; a poll may only ever add to it."""
    db = FakeDatabase()
    db.delete_rowcount = 17

    ingest_once(db, FakeSubgraphClient(), registry, token_settings(), frozen_now)

    assert db.executed == []


def test_no_statement_can_delete_hourly_rows() -> None:
    """Belt and braces: nothing in the statement set can prune stored history.

    This used to assert that the word DELETE appeared nowhere at all, which
    was a faithful proxy while every table here held market data. The
    derived-fill cache (`token_hours_fill`, stage 0 rollout step 3) broke
    the proxy without weakening the rule: invalidating an *estimate* is not
    pruning an *observation*, and a cached fill for an hour that has since
    been ingested is wrong rather than merely stale.

    So the assertion is now on the delete **target** rather than on the
    word. `delete_superseded_fills` joins `token_hours_data` in its USING
    clause and must still pass — it reads that table to decide what to
    remove from the cache — which is exactly the distinction a substring
    check cannot make.
    """
    import re

    from candlegraph.storage import statements_for

    statements = statements_for("candlegraph")

    assert not hasattr(statements, "delete_older_data")

    deletable = set()
    for name in vars(statements):
        if name.startswith("_"):
            continue
        rendered = str(getattr(statements, name))
        # `DELETE FROM `, then the schema Identifier, then `.<table>`.
        deletable.update(re.findall(r"DELETE FROM '\),\s*Identifier\('[^']+'\),\s*SQL\('\.(\w+)", rendered))

    assert deletable <= {"token_hours_fill"}, (
        f"only the derived-fill cache may be deleted from; found {sorted(deletable)}"
    )
    # And the rule it protects: the series tables are never a delete target.
    assert deletable.isdisjoint({"token_hours_data", "pool_hours_data", "signal", "ingest_run"})


def test_an_empty_run_is_still_a_success(registry: TokenRegistry, frozen_now: datetime) -> None:
    result = ingest_once(FakeDatabase(), FakeSubgraphClient(), registry, token_settings(), frozen_now)

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
    from candlegraph.ingestion import runner as ingest_module

    closed: list[bool] = []

    class RecordingClient(FakeSubgraphClient):
        def __init__(self, _settings: Any) -> None:
            super().__init__(tokens=[{"id": WBTC}])

        def __enter__(self) -> RecordingClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            closed.append(True)

    monkeypatch.setattr(ingest_module, "SubgraphClient", RecordingClient)

    result = ingest_module.run_ingest(FakeDatabase(), registry, token_settings(), frozen_now)

    assert result.tokens_written == 1
    assert closed == [True]


# --- pools -----------------------------------------------------------------

POOL_A = "0x" + "aa" * 20
POOL_B = "0x" + "bb" * 20


def pool_payload(pool_id: str) -> dict[str, Any]:
    return {
        "id": pool_id,
        "feeTier": "3000",
        "totalValueLockedUSD": "100",
        "token0": {"id": WBTC, "symbol": "WBTC"},
        "token1": {"id": SHIB, "symbol": "SHIB"},
    }


def pool_hour_rows(pool_id: str, hours: list[int]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"{pool_id}-{hour}",
            "periodStartUnix": hour * 3600,
            "open": "1",
            "high": "2",
            "low": "0",
            "close": "1.5",
            "tvlUSD": "100",
            "volumeUSD": "10",
            "feesUSD": "0.03",
            "txCount": "5",
        }
        for hour in hours
    ]


def pool_db(*pool_ids: str, **reads: Any) -> FakeDatabase:
    return FakeDatabase(get_pools_for_tokens=[{"id": pid} for pid in pool_ids], **reads)


def test_pool_discovery_stores_what_it_finds(registry: TokenRegistry, frozen_now: datetime) -> None:
    from candlegraph.ingestion.runner import discover_pools

    db = FakeDatabase()
    client = FakeSubgraphClient(pools=[pool_payload(POOL_A), pool_payload(POOL_B)])

    assert discover_pools(db, client, registry, 3, frozen_now) == 2
    assert client.pools_requested == [([WBTC, SHIB], 3)]
    [(label, rows)] = db.upserts
    assert label == "insert_pool"
    assert {row["id"] for row in rows} == {POOL_A, POOL_B}


def test_pool_cursors_resume_from_stored_pool_hours(frozen_now: datetime) -> None:
    from candlegraph.ingestion.runner import lookback_start, resolve_pool_cursors

    db = FakeDatabase(get_latest_pool_timestamps=[{"pool_id": POOL_A, "latest_unix": 1_700_000_000}])

    cursors = resolve_pool_cursors(db, (POOL_A, POOL_B), 7, frozen_now)

    assert cursors[POOL_A] == 1_700_000_000
    assert cursors[POOL_B] == lookback_start(7, frozen_now)


def test_pool_hours_are_written_per_page() -> None:
    from candlegraph.ingestion.runner import ingest_pool_hours

    db = FakeDatabase()
    client = FakeSubgraphClient(pool_pages={POOL_A: [pool_hour_rows(POOL_A, [1, 2])]})

    assert ingest_pool_hours(db, client, POOL_A, 0) == 2
    [(label, rows)] = db.upserts
    assert label == "insert_pool_hours"
    assert rows[0]["txCount"] == "5"


def test_a_full_run_ingests_pools_as_well(registry: TokenRegistry, frozen_now: datetime) -> None:
    db = pool_db(POOL_A)
    client = FakeSubgraphClient(
        tokens=[{"id": WBTC}],
        pages={WBTC: [hour_rows(WBTC, [1])]},
        pools=[pool_payload(POOL_A)],
        pool_pages={POOL_A: [pool_hour_rows(POOL_A, [1, 2])]},
    )

    result = ingest_once(db, client, registry, make_settings(), frozen_now)

    assert result.ok
    assert result.pools_written == 1
    assert result.pool_hours_written == 2
    assert result.pool_hours_by_pool == {POOL_A: 2}


def test_pools_are_skipped_when_disabled(registry: TokenRegistry, frozen_now: datetime) -> None:
    db = pool_db(POOL_A)
    client = FakeSubgraphClient(pools=[pool_payload(POOL_A)])

    result = ingest_once(db, client, registry, token_settings(), frozen_now)

    assert result.pools_written == 0
    assert client.pools_requested == []


def test_discovery_can_be_disabled_while_still_ingesting_known_pools(
    registry: TokenRegistry, frozen_now: datetime
) -> None:
    """`POOLS_PER_TOKEN=0` freezes the pool set without stopping its ingestion."""
    db = pool_db(POOL_A)
    client = FakeSubgraphClient(pool_pages={POOL_A: [pool_hour_rows(POOL_A, [1])]})

    result = ingest_once(db, client, registry, make_settings(pools_per_token=0), frozen_now)

    assert client.pools_requested == []
    assert result.pool_hours_written == 1


def test_a_failing_pool_does_not_stop_the_others(registry: TokenRegistry, frozen_now: datetime) -> None:
    db = pool_db(POOL_A, POOL_B)
    client = FakeSubgraphClient(
        pool_pages={POOL_B: [pool_hour_rows(POOL_B, [1])]}, fail={POOL_A: "pool exploded"}
    )

    result = ingest_once(db, client, registry, make_settings(pools_per_token=0), frozen_now)

    assert result.errors == {f"pool:{POOL_A}": "pool exploded"}
    assert result.pool_hours_by_pool == {POOL_B: 1}


def test_failed_discovery_is_recorded_but_tokens_still_ingest(
    registry: TokenRegistry, frozen_now: datetime
) -> None:
    db = FakeDatabase()
    client = FakeSubgraphClient(pages={WBTC: [hour_rows(WBTC, [1])]}, fail={"__pools__": "discovery down"})

    result = ingest_once(db, client, registry, make_settings(), frozen_now)

    assert result.errors == {"__pools__": "discovery down"}
    assert result.hours_written == 1


def test_no_tracked_pools_is_not_an_error(registry: TokenRegistry, frozen_now: datetime) -> None:
    result = ingest_once(FakeDatabase(), FakeSubgraphClient(), registry, make_settings(), frozen_now)

    assert result.ok
    assert result.pool_hours_written == 0


# --- result shape ----------------------------------------------------------


def test_rows_written_totals_every_table() -> None:
    result = IngestResult(tokens_written=1, hours_written=2, pools_written=3, pool_hours_written=4)

    assert result.rows_written == 10


def test_counts_match_the_ledger_columns() -> None:
    counts = IngestResult(tokens_written=1, hours_written=2, pool_hours_written=4).counts()

    assert counts == {
        "tokens_written": 1,
        "token_hours_written": 2,
        "pools_written": 0,
        "pool_hours_written": 4,
    }


# --- the ledger around a run -----------------------------------------------


def test_run_ingest_records_the_run_in_the_ledger(
    registry: TokenRegistry, frozen_now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from candlegraph.ingestion import runner as runner_module

    db = FakeDatabase()

    class Client(FakeSubgraphClient):
        def __init__(self, _settings: Any) -> None:
            super().__init__(pages={WBTC: [hour_rows(WBTC, [1])]}, tokens=[{"id": WBTC}])

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_exc: object) -> None:
            pass

    monkeypatch.setattr(runner_module, "SubgraphClient", Client)

    result = runner_module.run_ingest(db, registry, token_settings(), frozen_now)

    labels = [label for label, _ in db.executed]
    assert labels == ["start_ingest_run", "finish_ingest_run"]
    finish = next(p for label, p in db.executed if label == "finish_ingest_run")
    assert finish["status"] == "succeeded"
    assert finish["run_id"] == result.run_id
    assert finish["token_hours_written"] == 1


def test_a_run_that_cannot_start_is_recorded_as_failed(
    registry: TokenRegistry, frozen_now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing API key kills the run before any token is touched."""
    from candlegraph.ingestion import runner as runner_module

    db = FakeDatabase()

    def explode(_settings: Any) -> Any:
        raise SubgraphError("no endpoint configured")

    monkeypatch.setattr(runner_module, "SubgraphClient", explode)

    with pytest.raises(SubgraphError):
        runner_module.run_ingest(db, registry, token_settings(), frozen_now)

    finish = next(p for label, p in db.executed if label == "finish_ingest_run")
    assert finish["status"] == "failed"


def test_a_partly_failed_run_is_recorded_as_partial(
    registry: TokenRegistry, frozen_now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from candlegraph.ingestion import runner as runner_module

    db = FakeDatabase()

    class Client(FakeSubgraphClient):
        def __init__(self, _settings: Any) -> None:
            super().__init__(
                pages={SHIB: [hour_rows(SHIB, [1])]},
                tokens=[{"id": SHIB}],
                fail={WBTC: "gateway exploded"},
            )

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_exc: object) -> None:
            pass

    monkeypatch.setattr(runner_module, "SubgraphClient", Client)

    runner_module.run_ingest(db, registry, token_settings(), frozen_now)

    finish = next(p for label, p in db.executed if label == "finish_ingest_run")
    assert finish["status"] == "partial"


def test_backfill_overrides_the_configured_window(registry: TokenRegistry, frozen_now: datetime) -> None:
    """`candlegraph backfill --days 90` must reach past LOOKBACK_DAYS."""
    from candlegraph.ingestion.runner import lookback_start

    db = FakeDatabase()
    client = FakeSubgraphClient()

    ingest_once(db, client, registry, token_settings(lookback_days=7), frozen_now, lookback_days=90)

    assert client.cursors_seen[0][1] == lookback_start(90, frozen_now)


def test_an_inactive_token_is_not_polled_and_does_not_break_the_run(
    frozen_now: datetime, tmp_path: Path
) -> None:
    """The bug the unit tier missed and a real ingest run found immediately.

    `resolve_cursors` builds its map from the *active* addresses, so a
    loop over the whole registry looked up a cursor that was never
    created and died with a bare `KeyError` on the first inactive token.
    Nothing caught it because every fixture registry had all tokens
    active.
    """
    from candlegraph.tokens import load_tokens

    path = tmp_path / "tokens.yaml"
    path.write_text(
        """
tokens:
  - address: "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    symbol: WETH
    active: true
  - address: "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
    symbol: WBTC
    active: false
""",
        encoding="utf-8",
    )
    registry = load_tokens(path)

    result = ingest_once(FakeDatabase(), FakeSubgraphClient(), registry, token_settings(), frozen_now)

    assert result.ok, result.errors
    assert "WBTC" not in result.hours_by_symbol, "an inactive token is not polled"
    assert "WETH" in result.hours_by_symbol


# --- stages (harness rollout step 2) -----------------------------------------


def test_an_ingest_pass_records_four_stages_in_order(registry: TokenRegistry, frozen_now: datetime) -> None:
    """Same shape as an observer run: `ingest --progress` prints these lines."""
    client = FakeSubgraphClient(pages={WBTC: [hour_rows(WBTC, [1, 2])]}, tokens=[{"id": WBTC}])
    seen: list[str] = []

    result = ingest_once(
        FakeDatabase(), client, registry, make_settings(), frozen_now, on_stage=lambda r: seen.append(r.name)
    )

    assert seen == ["token_metadata", "token_hours", "pool_discovery", "pool_hours"]
    assert [s.name for s in result.stages] == seen
    hours = result.stages[1]
    assert hours.status == "ok" and hours.detail["rows"] == 2 and hours.detail["by_symbol"]["WBTC"] == 2
    assert all(s.duration_seconds is not None for s in result.stages)


def test_a_failing_token_marks_its_stage_as_an_error(registry: TokenRegistry, frozen_now: datetime) -> None:
    client = FakeSubgraphClient(tokens=[{"id": WBTC}], fail={WBTC: "gateway exploded"})

    result = ingest_once(FakeDatabase(), client, registry, token_settings(), frozen_now)

    by_name = {s.name: s for s in result.stages}
    assert by_name["token_hours"].status == "error" and by_name["token_hours"].detail["errors"] == 1
    assert by_name["token_metadata"].status == "ok"


def test_with_pools_off_the_pool_stages_are_skipped_not_missing(
    registry: TokenRegistry, frozen_now: datetime
) -> None:
    result = ingest_once(FakeDatabase(), FakeSubgraphClient(), registry, token_settings(), frozen_now)

    by_name = {s.name: s for s in result.stages}
    assert by_name["pool_discovery"].status == "skipped"
    assert by_name["pool_hours"].detail == {"reason": "INGEST_POOLS is off"}
