"""Stage records: what each node of a graph did, how long it took, in what order.

Before this, a graph run was a black box until its final Markdown: nothing
said which node was running when a run was slow, the report did not record
how long each read took or what it collected, and the database-down
short-circuit left no trace of the nodes it skipped. A :class:`StageRecord`
per node fixes that without touching the nodes themselves — :func:`staged`
wraps a node, times it, and derives a one-line detail from the update it
returned — so the collect functions stay pure and the records are a fact
about the run, not a finding (invariant 22: severity still comes from the
findings alone).

The records go three places: a ``Stages`` section in the report, the
``agent_run.detail`` JSON the UI reads, and an optional ``on_stage`` hook
that the CLI uses to print each stage as it finishes (``--progress``).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, TypeVar

from candlegraph.agents.base import now_utc

StageStatus = Literal["ok", "error", "skipped"]
S_contra = TypeVar("S_contra", bound=Mapping[str, Any], contravariant=True)


class StageNode(Protocol[S_contra]):
    """A node as LangGraph types it: the state in, an update out."""

    def __call__(self, state: S_contra) -> dict[str, Any]: ...


@dataclass(frozen=True)
class StageRecord:
    """One node's run."""

    name: str
    status: str
    """``ok``, ``error`` (the node recorded an error and the graph went on), or ``skipped``."""
    started_at: datetime | None
    duration_seconds: float | None
    detail: dict[str, Any] = field(default_factory=dict)
    """Small, JSON-safe numbers: rows read, symbols judged, findings by severity."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "duration_seconds": self.duration_seconds,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StageRecord:
        started = data.get("started_at")
        duration = data.get("duration_seconds")
        return cls(
            name=str(data["name"]),
            status=str(data.get("status", "ok")),
            started_at=datetime.fromisoformat(started) if isinstance(started, str) else None,
            duration_seconds=float(duration) if duration is not None else None,
            detail=dict(data.get("detail") or {}),
        )


StageHook = Callable[[StageRecord], None]
Summarise = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]


def no_detail(_state: Mapping[str, Any], _update: Mapping[str, Any]) -> dict[str, Any]:
    return {}


def staged[S: Mapping[str, Any]](
    name: str,
    node: Callable[[S], dict[str, Any]],
    summarise: Summarise = no_detail,
    on_stage: StageHook | None = None,
    clock: Callable[[], datetime] = now_utc,
) -> StageNode[S]:
    """Wrap a graph node so it also appends a :class:`StageRecord` to ``stages``.

    The node is unchanged: it still receives the state and returns its
    update. The wrapper times it, grades it ``error`` when the update carries
    ``errors`` (the collect nodes' convention), summarises the update with
    ``summarise(state, update)``, calls ``on_stage`` if given, and adds the
    record under the ``stages`` key, which the state must reduce with
    ``operator.add``.
    """

    def run(state: S) -> dict[str, Any]:
        started = clock()
        t0 = time.perf_counter()
        update = node(state)
        record = StageRecord(
            name=name,
            status="error" if update.get("errors") else "ok",
            started_at=started,
            duration_seconds=round(time.perf_counter() - t0, 3),
            detail=summarise(state, update),
        )
        if on_stage is not None:
            on_stage(record)
        return {**update, "stages": [record]}

    run.__name__ = name
    return run


def complete_stages(
    records: Sequence[StageRecord], expected: Sequence[str], reason: str
) -> list[StageRecord]:
    """The records plus a ``skipped`` row for every expected stage that never ran.

    A short-circuit (the database is down, so nothing else can be read)
    should be visible as skipped stages, not as rows that are simply absent.
    """
    ran = {r.name for r in records}
    out = list(records)
    for name in expected:
        if name not in ran:
            out.append(StageRecord(name, "skipped", None, None, {"reason": reason}))
    return out


def describe(detail: Mapping[str, Any]) -> str:
    """``k=v, k=v`` with numbers trimmed, for one line of terminal."""
    parts = []
    for key, value in detail.items():
        if isinstance(value, float):
            text = f"{value:.3g}"
        elif isinstance(value, (list, tuple)):
            text = ",".join(str(v) for v in value) or "-"
        else:
            text = str(value)
        parts.append(f"{key}={text}")
    return ", ".join(parts)


def format_stage(record: StageRecord) -> str:
    """One line per stage, aligned, for ``--progress``."""
    mark = {"ok": "ok", "error": "ERR", "skipped": "skip"}.get(record.status, record.status)
    secs = f"{record.duration_seconds:6.2f}s" if record.duration_seconds is not None else "     - "
    return f"{mark:<4} {record.name:<16} {secs}  {describe(record.detail)}".rstrip()


def stages_section(records: Sequence[StageRecord]) -> str:
    """The ``Stages`` table of a report."""
    if not records:
        return ""
    lines = ["| stage | status | seconds | detail |", "|---|---|---:|---|"]
    for r in records:
        secs = f"{r.duration_seconds:.2f}" if r.duration_seconds is not None else "-"
        lines.append(f"| {r.name} | {r.status} | {secs} | {describe(r.detail).replace('|', '/')} |")
    return "\n".join(lines)
