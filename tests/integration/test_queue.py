"""The work queue against real SQL: dedup, SKIP LOCKED, leases, and the lock.

Every property this file tests is a property of Postgres rather than of our
Python — a unique index, a row-level lock, an advisory lock — so a fake
database can say nothing useful about any of them. This is the tier that
decides whether "exactly one hourly run" is true.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from candlegraph.harness import queue
from candlegraph.harness.events import pipeline_lock
from candlegraph.storage import DatabaseManager

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def queue_db(db: DatabaseManager) -> DatabaseManager:
    db.execute(f'TRUNCATE "{db.schema}".run_queue')
    return db


def ask(db: DatabaseManager, *, pipeline: str = "hourly", key: str = "hour:2026-11-18T12", **kw: Any):
    return queue.enqueue(
        db, pipeline=pipeline, version=1, dedup_key=key, trigger=kw.pop("trigger", "event"), now=NOW, **kw
    )


def test_the_same_work_asked_for_twice_is_queued_once(queue_db: DatabaseManager) -> None:
    """The whole of "exactly one hourly run per bar hour", in one assertion.

    Twelve successful ingests inside an hour all produce this key.
    """
    first = ask(queue_db)
    second = ask(queue_db)

    assert first is not None
    assert second is None  # the dedup key did its job
    assert len(queue.recent(queue_db)) == 1


def test_a_different_hour_is_different_work(queue_db: DatabaseManager) -> None:
    assert ask(queue_db, key="hour:2026-11-18T12") is not None
    assert ask(queue_db, key="hour:2026-11-18T13") is not None

    assert len(queue.recent(queue_db)) == 2


def test_two_pipelines_may_share_a_dedup_key(queue_db: DatabaseManager) -> None:
    """The key is unique per pipeline, not globally: both run for the same hour."""
    assert ask(queue_db, pipeline="hourly") is not None
    assert ask(queue_db, pipeline="other") is not None


def test_a_claim_takes_the_row_and_a_second_claim_finds_nothing(queue_db: DatabaseManager) -> None:
    """`FOR UPDATE SKIP LOCKED`: two dispatchers never take the same row."""
    ask(queue_db)

    first = queue.claim(queue_db, claimed_by="one", lease_seconds=120, now=NOW)
    second = queue.claim(queue_db, claimed_by="two", lease_seconds=120, now=NOW)

    assert first is not None and first.claimed_by == "one"
    assert first.attempts == 1
    assert second is None


def test_two_dispatchers_take_two_different_rows(queue_db: DatabaseManager) -> None:
    ask(queue_db, key="a")
    ask(queue_db, key="b")

    first = queue.claim(queue_db, claimed_by="one", lease_seconds=120, now=NOW)
    second = queue.claim(queue_db, claimed_by="two", lease_seconds=120, now=NOW)

    assert first is not None and second is not None
    assert first.id != second.id


def test_a_row_that_is_not_due_yet_is_not_claimed(queue_db: DatabaseManager) -> None:
    ask(queue_db, available_at=NOW + timedelta(minutes=5))

    assert queue.claim(queue_db, claimed_by="one", lease_seconds=120, now=NOW) is None
    assert queue.claim(queue_db, claimed_by="one", lease_seconds=120, now=NOW + timedelta(minutes=6))


def test_completing_a_row_points_it_at_the_run_it_produced(queue_db: DatabaseManager) -> None:
    ask(queue_db)
    entry = queue.claim(queue_db, claimed_by="one", lease_seconds=120, now=NOW)
    assert entry is not None

    queue.complete(queue_db, entry, run_id="33333333-3333-3333-3333-333333333333", now=NOW)

    (row,) = queue.recent(queue_db)
    assert row["state"] == "done"
    assert str(row["run_id"]).startswith("33333333")
    assert row["claimed_by"] is None


def test_a_completed_row_does_not_block_the_next_hour(queue_db: DatabaseManager) -> None:
    """`done` is a terminal state, not a tombstone: the key is per hour anyway."""
    ask(queue_db, key="hour:12")
    entry = queue.claim(queue_db, claimed_by="one", lease_seconds=120, now=NOW)
    assert entry is not None
    queue.complete(queue_db, entry, run_id=None, now=NOW)

    assert ask(queue_db, key="hour:13") is not None


def test_a_completed_row_is_never_asked_for_again_in_the_same_hour(queue_db: DatabaseManager) -> None:
    """The other half: a finished hour must not be re-queued by the next tick."""
    ask(queue_db, key="hour:12")
    entry = queue.claim(queue_db, claimed_by="one", lease_seconds=120, now=NOW)
    assert entry is not None
    queue.complete(queue_db, entry, run_id=None, now=NOW)

    assert ask(queue_db, key="hour:12") is None


# --- crashes -----------------------------------------------------------------


def test_an_expired_lease_returns_the_row_to_the_queue(queue_db: DatabaseManager) -> None:
    """The dispatcher died mid-run; another one must be able to pick it up."""
    ask(queue_db)
    queue.claim(queue_db, claimed_by="dead", lease_seconds=60, now=NOW)

    changed = queue.reap(queue_db, max_claims=5, now=NOW + timedelta(minutes=5))

    assert [row["state"] for row in changed] == ["queued"]
    assert queue.claim(queue_db, claimed_by="alive", lease_seconds=60, now=NOW + timedelta(minutes=5))


def test_a_live_lease_is_left_alone(queue_db: DatabaseManager) -> None:
    ask(queue_db)
    queue.claim(queue_db, claimed_by="busy", lease_seconds=600, now=NOW)

    assert queue.reap(queue_db, max_claims=5, now=NOW + timedelta(minutes=1)) == []


def test_renewing_keeps_a_long_run_s_row(queue_db: DatabaseManager) -> None:
    ask(queue_db)
    entry = queue.claim(queue_db, claimed_by="busy", lease_seconds=60, now=NOW)
    assert entry is not None

    assert queue.renew(queue_db, entry, lease_seconds=600, now=NOW + timedelta(seconds=30)) == 1
    assert queue.reap(queue_db, max_claims=5, now=NOW + timedelta(minutes=5)) == []


def test_a_renewal_after_the_lease_was_lost_changes_nothing(queue_db: DatabaseManager) -> None:
    """A process that wakes from a long pause must not reclaim what was taken."""
    ask(queue_db)
    entry = queue.claim(queue_db, claimed_by="slow", lease_seconds=60, now=NOW)
    assert entry is not None
    queue.reap(queue_db, max_claims=5, now=NOW + timedelta(minutes=5))

    assert queue.renew(queue_db, entry, lease_seconds=600, now=NOW + timedelta(minutes=5)) == 0


def test_a_row_that_keeps_killing_its_dispatcher_goes_dead(queue_db: DatabaseManager) -> None:
    """Invariant 8 in spirit: never retry forever in place."""
    ask(queue_db)
    for minute in range(3):
        when = NOW + timedelta(minutes=minute * 10)
        queue.claim(queue_db, claimed_by=f"doomed{minute}", lease_seconds=60, now=when)
        changed = queue.reap(queue_db, max_claims=3, now=when + timedelta(minutes=5))

    assert [row["state"] for row in changed] == ["dead"]
    assert queue.claim(queue_db, claimed_by="next", lease_seconds=60, now=NOW + timedelta(hours=1)) is None


def test_releasing_puts_a_row_back_without_losing_the_attempt_count(queue_db: DatabaseManager) -> None:
    ask(queue_db)
    entry = queue.claim(queue_db, claimed_by="one", lease_seconds=120, now=NOW)
    assert entry is not None

    queue.release(queue_db, entry, reason="locked elsewhere", delay_seconds=30, now=NOW)

    (row,) = queue.recent(queue_db)
    assert row["state"] == "queued" and row["attempts"] == 1
    assert row["last_error"] == "locked elsewhere"


# --- the advisory lock ----------------------------------------------------------


def test_one_holder_at_a_time_per_pipeline(queue_db: DatabaseManager) -> None:
    """What stops a manual `candlegraph run` colliding with the dispatcher."""
    other = DatabaseManager(dsn=queue_db.dsn, schema=queue_db.schema, min_connections=1, max_connections=2)
    try:
        with pipeline_lock(queue_db, "hourly") as mine:
            assert mine is True
            with pipeline_lock(other, "hourly") as theirs:
                assert theirs is False
        # Released on the way out, so the next caller gets it.
        with pipeline_lock(other, "hourly") as after:
            assert after is True
    finally:
        other.close()


def test_two_different_pipelines_do_not_block_each_other(queue_db: DatabaseManager) -> None:
    other = DatabaseManager(dsn=queue_db.dsn, schema=queue_db.schema, min_connections=1, max_connections=2)
    try:
        with pipeline_lock(queue_db, "hourly") as mine, pipeline_lock(other, "daily") as theirs:
            assert mine and theirs
    finally:
        other.close()


# --- pruning --------------------------------------------------------------------


def test_finished_rows_are_pruned_and_live_ones_are_not(queue_db: DatabaseManager) -> None:
    ask(queue_db, key="old")
    entry = queue.claim(queue_db, claimed_by="one", lease_seconds=60, now=NOW)
    assert entry is not None
    queue.complete(queue_db, entry, run_id=None, now=NOW - timedelta(days=30))
    ask(queue_db, key="new")

    deleted = queue.prune(queue_db, older_than_days=7, now=NOW)

    assert deleted == 1
    assert [row["dedup_key"] for row in queue.recent(queue_db)] == ["new"]
