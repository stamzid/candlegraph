"""Pipeline specs: several registered jobs run as one unit.

A **pipeline** is a named, ordered list of registered jobs that run together —
*validate, then analyse, then signal* — so that a failed check stops the steps
behind it instead of letting them work from bad data. The format is YAML and
the design is ``docs/harness/pipelines/DESIGN.md``; this module is rollout
step 3 of it: the spec, the loader, and running one through ``run_job``.

Three ideas carry the whole module.

**The whole file is rejected, with a line number.** A spec is a public format
that people write by hand, so a typo must never become a silently ignored
setting. Every model forbids unknown keys, every rule in the design's §5.2 is
checked, and :class:`PipelineError` reports *all* the problems at once, each
as ``path:line: message``. ``candlegraph run --check`` runs exactly this and
nothing else.

**A pipeline run is a run.** Invariant 27 says every run goes through
``run_job`` with a ledger row opened before and closed after it. So a spec is
turned into a :class:`~candlegraph.harness.jobs.Job` and handed to ``run_job``
like any other: the parent row in the generic ``run`` table is written by the
ordinary ledger hook, each step is another ``run_job`` call carrying
``parent_run_id``, and ``run_log`` shows the family without any new query. A
step is also recorded as a :class:`~candlegraph.harness.stages.StageRecord`,
which is what lets the dashboard draw a pipeline run with the stage strip it
already has.

**Steps hand nothing to each other.** Each reads what it needs from the
database, bounded by the run's as-of time (design §6.3). The executor supplies
``as_of`` in each step's params; a spec that sets it itself is refused, because
one run must see one moment.

Not here, and deliberately: the durable queue, leases, resume, retries and
event triggers. Those arrive in rollout steps 4 and 5, where a scheduler or a
dispatcher is the thing that produces work for them. What is here is what a
foreground ``candlegraph run`` needs.
"""

from __future__ import annotations

import random
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from candlegraph.harness.errors import ErrorClass, classify
from candlegraph.harness.jobs import JOBS, NEVER, Job, JobContext, run_ledger
from candlegraph.harness.runs import MANUAL, Outcome, Run, RunOutcome, run_job
from candlegraph.harness.stages import StageHook, StageRecord, now_utc
from candlegraph.settings import Settings
from candlegraph.utils.logging_utils import service_logger

if TYPE_CHECKING:
    from candlegraph.storage import DatabaseManager

# What a step's outcome does to the rest of the pipeline (design §6.4).
OnFailure = Literal["stop", "continue", "skip_rest"]
OnPartial = Literal["continue", "stop"]

# The prose roles a step may ask for; see docs/harness/models/DESIGN.md. Only
# `agent` and `knowledge` resolve to anything today — `analyst` is declared
# there and has no model behind it yet.
MODEL_ROLES = ("agent", "knowledge", "analyst")

# Params the executor supplies, which a spec may therefore not set. `as_of`
# fixes the moment the whole run sees (§6.3); `deadline` and `attempt` are
# what the executor is telling the step about its own retry and timeout, and
# a step that could overwrite them could grant itself more time than the
# pipeline has.
RESERVED_PARAMS = frozenset({"as_of", "deadline", "attempt"})

# Worst-first, so a pipeline's outcome is a max over its steps.
_SEVERITY = {Outcome.SUCCEEDED: 0, Outcome.PARTIAL: 1, Outcome.FAILED: 2}

_DURATION = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>s|m|h)$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600}


# --- errors ------------------------------------------------------------------


@dataclass(frozen=True)
class SpecProblem:
    """One reason a spec was rejected, and where in the file it is."""

    line: int | None
    message: str

    def render(self, path: Path) -> str:
        where = f"{path}:{self.line}" if self.line is not None else str(path)
        return f"{where}: {self.message}"


class PipelineError(ValueError):
    """A spec is malformed, names something unregistered, or breaks a rule.

    Carries every problem found, not the first: a person fixing a spec should
    need one run of ``--check``, not one per mistake.
    """

    def __init__(self, path: Path, problems: Sequence[SpecProblem]) -> None:
        self.path = path
        self.problems = tuple(problems)
        super().__init__("\n".join(problem.render(path) for problem in self.problems))


# --- the spec ----------------------------------------------------------------


def _duration(value: Any) -> float:
    """``30s`` / ``5m`` / ``2h`` — or a bare number of seconds — as seconds."""
    if isinstance(value, bool):  # bool is an int; `timeout: true` is not 1 second
        raise ValueError("must be a duration such as 30s, 5m or 2h")
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        match = _DURATION.match(value.strip())
        if match is None:
            raise ValueError(f"{value!r} is not a duration such as 30s, 5m or 2h")
        seconds = float(match.group("value")) * _DURATION_UNITS[match.group("unit")]
    else:
        raise ValueError("must be a duration such as 30s, 5m or 2h")
    if seconds <= 0:
        raise ValueError("must be greater than zero")
    return seconds


def _optional_duration(value: Any) -> float | None:
    return None if value is None else _duration(value)


# A validator on the field rather than on the model, so a bad duration is
# reported against the field's own location and lands on the line holding it.
Duration = Annotated[float, BeforeValidator(_duration)]
OptionalDuration = Annotated[float | None, BeforeValidator(_optional_duration)]


class _Spec(BaseModel):
    """Every spec model forbids unknown keys and is immutable once loaded."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Retry(_Spec):
    """How many times a transient failure is retried, and how long between.

    Parsed and carried from rollout step 3; applied in step 4, which is where
    error classification and backoff live (design §6.5).
    """

    attempts: int = Field(default=1, ge=1, le=10)
    backoff: Duration = 30.0
    max_backoff: Duration = 300.0

    @model_validator(mode="after")
    def _ordered(self) -> Retry:
        if self.max_backoff < self.backoff:
            raise ValueError("max_backoff must not be shorter than backoff")
        return self


class EventTrigger(_Spec):
    """Start this pipeline when a job finishes with one of these outcomes."""

    job: str
    outcome: tuple[Outcome, ...] = (Outcome.SUCCEEDED,)
    once_per: Literal["hour", "day", "run"] = "hour"
    """How often a matching event may start a run.

    The default is **not** ``run``, and that is the field's whole point.
    Ingest polls every five minutes, so "one hourly pipeline per successful
    ingest" would be twelve runs an hour, each writing signals and explaining
    them. ``hour`` is the bar grain the pipeline works at, so twelve matching
    events inside one hour produce one run. ``run`` is for a job whose every
    completion genuinely deserves its own pipeline.
    """

    @model_validator(mode="after")
    def _finished_outcomes_only(self) -> EventTrigger:
        if Outcome.RUNNING in self.outcome:
            raise ValueError("'running' is not an outcome a run finishes with")
        if not self.outcome:
            raise ValueError("an event trigger needs at least one outcome")
        return self

    def matches(self, job: str, outcome: str) -> bool:
        return job == self.job and outcome in {value.value for value in self.outcome}


class ScheduleTrigger(_Spec):
    """Start this pipeline every ``every`` seconds."""

    every: Duration


class Trigger(_Spec):
    """One trigger: exactly one of ``event`` or ``schedule``."""

    event: EventTrigger | None = None
    schedule: ScheduleTrigger | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Trigger:
        given = [name for name, value in (("event", self.event), ("schedule", self.schedule)) if value]
        if len(given) != 1:
            raise ValueError("a trigger must set exactly one of 'event' or 'schedule'")
        return self


class Defaults(_Spec):
    """Values every step inherits unless it overrides them."""

    retry: Retry = Retry()
    timeout: OptionalDuration = None


class StepSpec(_Spec):
    """One step: a registered job, its params, and what its outcome does next."""

    id: str = Field(min_length=1)
    job: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    on_failure: OnFailure = "stop"
    on_partial: OnPartial = "continue"
    timeout: OptionalDuration = None
    retry: Retry | None = None
    model_role: str | None = None

    # A field validator rather than a model one, so the error carries the
    # field's own location and lands on the line that holds it.
    @field_validator("model_role")
    @classmethod
    def _known_role(cls, value: str | None) -> str | None:
        if value is not None and value not in MODEL_ROLES:
            raise ValueError(f"unknown model_role {value!r}; known: {', '.join(MODEL_ROLES)}")
        return value


class PipelineSpec(_Spec):
    """A loaded, validated pipeline. Only :func:`load_pipeline` builds one."""

    name: str = Field(min_length=1)
    description: str = ""
    version: int = Field(default=1, ge=1)
    triggers: tuple[Trigger, ...] = ()
    concurrency: Literal["one_at_a_time"] = "one_at_a_time"
    deadline: OptionalDuration = None
    defaults: Defaults = Defaults()
    # No `min_length` here, and "no steps" is a semantic check instead: pydantic
    # drops an item that fails validation and would then also complain the list
    # is empty, which buries the real mistake under a consequence of it.
    steps: tuple[StepSpec, ...]

    def timeout_for(self, step: StepSpec) -> float | None:
        """The step's own timeout, else the file's default, else none."""
        return step.timeout if step.timeout is not None else self.defaults.timeout

    def retry_for(self, step: StepSpec) -> Retry:
        return step.retry if step.retry is not None else self.defaults.retry

    @property
    def events(self) -> tuple[EventTrigger, ...]:
        return tuple(t.event for t in self.triggers if t.event is not None)

    @property
    def schedules(self) -> tuple[ScheduleTrigger, ...]:
        return tuple(t.schedule for t in self.triggers if t.schedule is not None)

    @property
    def emits(self) -> frozenset[str]:
        """The job names a run of this pipeline puts in the ledger.

        Its own name — a pipeline run is a `run` row like any other — and
        every step's. This is what a cycle check compares triggers against.
        """
        return frozenset({self.name, *(step.job for step in self.steps)})


# --- line numbers ------------------------------------------------------------


def _line_map(text: str) -> dict[tuple[str | int, ...], int]:
    """Map every path in the document to the 1-based line it is written on.

    PyYAML's ``compose`` keeps the marks ``safe_load`` throws away, so this
    costs one extra parse and no new dependency. A mapping key is recorded at
    the key's own line rather than its value's, which is what a reader needs
    when the complaint is "unknown key" or "wrong type".
    """
    lines: dict[tuple[str | int, ...], int] = {}

    def walk(node: yaml.Node, path: tuple[str | int, ...]) -> None:
        lines.setdefault(path, node.start_mark.line + 1)
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                child = (*path, str(key.value))
                walk(value, child)
                lines[child] = key.start_mark.line + 1
        elif isinstance(node, yaml.SequenceNode):
            for index, item in enumerate(node.value):
                walk(item, (*path, index))

    root = yaml.compose(text)
    if root is not None:
        walk(root, ())
    return lines


def _line_for(lines: Mapping[tuple[str | int, ...], int], loc: Sequence[Any]) -> int | None:
    """The line of the deepest part of ``loc`` the document actually has.

    A pydantic error on ``steps.0.params.window`` points at ``window`` if the
    file has it, and at the step otherwise — which is the case when the
    complaint is that something is missing.
    """
    path: list[str | int] = [part for part in loc if isinstance(part, (str, int))]
    while path:
        found = lines.get(tuple(path))
        if found is not None:
            return found
        path.pop()
    return lines.get(())


# --- loading -----------------------------------------------------------------


def load_pipeline(path: Path) -> PipelineSpec:
    """Read, parse and fully validate a spec file.

    Raises:
        PipelineError: The file is unreadable, is not YAML, is not a mapping,
            or breaks any rule in the design's §5.2. Every problem found is
            carried, each with its line.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PipelineError(path, [SpecProblem(None, f"cannot read the spec: {exc}")]) from exc

    try:
        lines = _line_map(text)
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        detail = getattr(exc, "problem", None) or "the file is not valid YAML"
        line = mark.line + 1 if mark is not None else None
        raise PipelineError(path, [SpecProblem(line, f"not valid YAML: {detail}")]) from exc

    if document is None:
        raise PipelineError(path, [SpecProblem(1, "the spec is empty")])
    if not isinstance(document, dict):
        raise PipelineError(
            path, [SpecProblem(1, f"a spec must be a mapping, got {type(document).__name__}")]
        )

    try:
        spec = PipelineSpec.model_validate(document)
    except ValidationError as exc:
        raise PipelineError(path, _problems_from(exc, lines)) from exc

    problems = list(_semantic_problems(spec, lines))
    if problems:
        raise PipelineError(path, problems)
    return spec


def _problems_from(error: ValidationError, lines: Mapping[tuple[str | int, ...], int]) -> list[SpecProblem]:
    """Pydantic's errors as ours, deepest location first, de-duplicated."""
    problems: list[SpecProblem] = []
    seen: set[tuple[int | None, str]] = set()
    for item in error.errors():
        loc = item["loc"]
        where = ".".join(str(part) for part in loc)
        message = item["msg"]
        if item["type"] == "extra_forbidden":
            message = f"unknown key {loc[-1]!r}" if loc else "unknown key"
        elif where:
            message = f"{where}: {message}"
        key = (_line_for(lines, loc), message)
        if key not in seen:
            seen.add(key)
            problems.append(SpecProblem(key[0], key[1]))
    return problems


def _semantic_problems(
    spec: PipelineSpec, lines: Mapping[tuple[str | int, ...], int]
) -> Iterator[SpecProblem]:
    """Everything the shape of the document cannot say (design §5.2).

    These run only after the models validate, so each one can assume the field
    it reads exists and has the right type.
    """
    if not spec.steps:
        yield SpecProblem(lines.get(("steps",), 1), "a pipeline needs at least one step")

    if spec.name in JOBS:
        yield SpecProblem(
            lines.get(("name",)),
            f"a pipeline may not be named {spec.name!r}: that is a registered job, and the two "
            "share the `job` column of run_log",
        )

    seen: dict[str, int] = {}
    for index, step in enumerate(spec.steps):
        at = lines.get(("steps", index, "id"), lines.get(("steps", index)))
        if step.id in seen:
            yield SpecProblem(at, f"duplicate step id {step.id!r}, first used at step {seen[step.id] + 1}")
        seen[step.id] = index
        yield from _step_problems(spec, step, index, lines)

    for index, trigger in enumerate(spec.triggers):
        # A pipeline triggered by anything it emits never stops — its own name
        # (a pipeline run writes a ledger row too) or any of its steps. Loops
        # through a *second* pipeline need every spec in hand, and are
        # `check_cycles`' business; this one can name the line.
        if trigger.event is not None and trigger.event.job in spec.emits:
            what = (
                "names this pipeline"
                if trigger.event.job == spec.name
                else f"names {trigger.event.job!r}, which is one of its own steps"
            )
            yield SpecProblem(
                lines.get(("triggers", index, "event")),
                f"the event trigger {what}, so each run would start the next one",
            )

    if spec.deadline is not None:
        timeouts = [spec.timeout_for(step) for step in spec.steps]
        if all(value is not None for value in timeouts):
            total = sum(value for value in timeouts if value is not None)
            if total > spec.deadline:
                yield SpecProblem(
                    lines.get(("deadline",)),
                    f"the deadline ({spec.deadline:g}s) is shorter than the step timeouts it has to "
                    f"cover ({total:g}s), so it would always fire",
                )


def _step_problems(
    spec: PipelineSpec,
    step: StepSpec,
    index: int,
    lines: Mapping[tuple[str | int, ...], int],
) -> Iterator[SpecProblem]:
    at_job = lines.get(("steps", index, "job"), lines.get(("steps", index)))
    job = JOBS.get(step.job)
    if job is None:
        yield SpecProblem(
            at_job,
            f"unknown job {step.job!r}; registered: {', '.join(sorted(JOBS))}",
        )
        return

    if "market_data" in job.writes:
        yield SpecProblem(
            at_job,
            f"{step.job!r} writes market data, and a pipeline never does: ingest belongs to the "
            "worker and a pipeline reacts to it (invariant 21)",
        )
    if not job.idempotent:
        yield SpecProblem(
            at_job,
            f"{step.job!r} is not declared idempotent, so re-running it after a crash would not be "
            "safe; only an idempotent job can be a step",
        )

    reserved = sorted(RESERVED_PARAMS.intersection(step.params))
    if reserved:
        yield SpecProblem(
            lines.get(("steps", index, "params", reserved[0]), at_job),
            f"{reserved[0]!r} is set by the pipeline, not by a step: every step of one run reads "
            "the same moment",
        )

    yield from _param_problems(step, index, job, lines)


def _param_problems(
    step: StepSpec, index: int, job: Job, lines: Mapping[tuple[str | int, ...], int]
) -> Iterator[SpecProblem]:
    at_params = lines.get(("steps", index, "params"), lines.get(("steps", index)))
    given = {key: value for key, value in step.params.items() if key not in RESERVED_PARAMS}
    if job.params is None:
        if given:
            yield SpecProblem(
                at_params,
                f"{step.job!r} takes no parameters, but {', '.join(sorted(given))} "
                f"{'was' if len(given) == 1 else 'were'} given",
            )
        return
    try:
        job.params.model_validate(given)
    except ValidationError as exc:
        # Reported against the *spec's* path, not the param model's, so the
        # line points at the file the person is editing.
        for item in exc.errors():
            loc = ("steps", index, "params", *item["loc"])
            where = ".".join(str(part) for part in item["loc"])
            message = (
                f"unknown parameter {item['loc'][-1]!r}"
                if item["type"] == "extra_forbidden" and item["loc"]
                else f"{where}: {item['msg']}".lstrip(": ")
            )
            yield SpecProblem(_line_for(lines, loc) or at_params, f"{step.job}: {message}")


# --- triggers: what makes a pipeline run ---------------------------------------


def event_dedup_key(trigger: EventTrigger, *, occurred_at: datetime, cause_run_id: str) -> str:
    """The idempotency key for one matching event.

    This single value is what "exactly one run per trigger" reduces to: the
    queue's unique index does the rest, so the property survives a restart,
    two dispatchers, and the same window being scanned twice.
    """
    if trigger.once_per == "run":
        return f"run:{cause_run_id}"
    moment = occurred_at.astimezone(UTC)
    if trigger.once_per == "day":
        return f"day:{moment:%Y-%m-%d}"
    return f"hour:{moment:%Y-%m-%dT%H}"


def schedule_dedup_key(trigger: ScheduleTrigger, *, now: datetime) -> str:
    """The idempotency key for a schedule trigger: which interval we are in.

    A bucket rather than a timestamp, so the safety net can be re-evaluated
    every tick without ever asking for the same interval twice.
    """
    every = max(1, int(trigger.every))
    return f"every{every}:{int(now.astimezone(UTC).timestamp()) // every}"


def manual_dedup_key(now: datetime) -> str:
    """A key that never collides: a person asking twice means twice."""
    return f"manual:{now.astimezone(UTC).isoformat()}:{uuid.uuid4().hex[:8]}"


def ui_dedup_key(now: datetime) -> str:
    """The key for a *Run now* click: one run per pipeline per minute.

    Deliberately not :func:`manual_dedup_key`. A browser double-click, a
    second tab, or Streamlit re-running a callback is not a person asking
    twice, and each would otherwise start another full pipeline. A minute is
    long enough to absorb those and short enough that a deliberate second run
    is only a minute away. The pipeline name is part of the queue's unique
    key already, so clicking two different pipelines in one minute runs both.
    """
    return f"ui:{now.astimezone(UTC):%Y-%m-%dT%H:%M}"


class PipelineCycleError(ValueError):
    """Two or more specs would trigger each other for ever.

    Not a :class:`PipelineError`: a cycle is a property of a *set* of files,
    so there is no single file and line to point at.
    """


def check_cycles(specs: Sequence[PipelineSpec]) -> None:
    """Refuse a set of specs where a run would cause a run that causes it.

    A pipeline *emits* its own name and its steps' job names (every one of
    them writes a ledger row), and it is *triggered by* the jobs its event
    triggers name. An edge A → B means "a run of A can start B". A cycle in
    that graph is a pipeline that never stops, and a load-time refusal is the
    only cheap place to catch it — at run time it is a full disk.

    The single-file half of this (a trigger naming its own pipeline) is also
    checked by the loader, where it can carry a line number.

    Raises:
        PipelineCycleError: a cycle exists, named in the message.
    """
    edges: dict[str, set[str]] = {spec.name: set() for spec in specs}
    for source in specs:
        for target in specs:
            if any(trigger.job in source.emits for trigger in target.events):
                edges[source.name].add(target.name)

    # Depth-first search, colouring nodes: grey = on the current path.
    white, grey, black = 0, 1, 2
    colour = dict.fromkeys(edges, white)

    def walk(node: str, path: list[str]) -> None:
        colour[node] = grey
        for nxt in sorted(edges[node]):
            if colour[nxt] == grey:
                cycle = [*path[path.index(nxt) :], nxt] if nxt in path else [node, nxt]
                raise PipelineCycleError(
                    "these pipelines trigger each other in a loop: "
                    + " -> ".join(cycle)
                    + ". A run would cause a run that causes it; break the chain by removing "
                    "one of the event triggers."
                )
            if colour[nxt] == white:
                walk(nxt, [*path, nxt])
        colour[node] = black

    for spec in specs:
        if colour[spec.name] == white:
            walk(spec.name, [spec.name])


def load_pipelines(paths: Sequence[Path]) -> tuple[PipelineSpec, ...]:
    """Load several specs and check what only a set of them can be checked for.

    Each file is validated on its own first, so a mistake inside one still
    reports with its own line; the cycle check runs over whatever loaded.

    Raises:
        PipelineError: one of the files is invalid.
        PipelineCycleError: together they would trigger each other for ever.
    """
    specs = tuple(load_pipeline(path) for path in paths)
    duplicates = sorted({s.name for s in specs if [x.name for x in specs].count(s.name) > 1})
    if duplicates:
        raise PipelineCycleError(
            f"two specs share the name(s) {', '.join(duplicates)}; a pipeline's name is its "
            "identity in run_log and in the queue's dedup key"
        )
    check_cycles(specs)
    return specs


def load_pipeline_dir(directory: Path) -> tuple[PipelineSpec, ...]:
    """Every ``*.yaml`` in a directory, loaded and cross-checked, newest rules applied."""
    paths = sorted(p for p in directory.glob("*.yaml") if p.is_file())
    return load_pipelines(paths)


def resolve_spec(reference: str, settings: Settings) -> Path:
    """A path as written, or the name of a spec in ``PIPELINES_DIR``.

    ``candlegraph run pipelines/hourly.yaml`` and ``candlegraph run hourly``
    both work; the second is what a person types twice a day.
    """
    candidate = Path(reference)
    if candidate.suffix in {".yaml", ".yml"} or candidate.exists():
        return candidate
    for suffix in (".yaml", ".yml"):
        named = settings.pipelines_path / f"{reference}{suffix}"
        if named.exists():
            return named
    return settings.pipelines_path / f"{reference}.yaml"


# --- running -----------------------------------------------------------------


@dataclass(frozen=True)
class StepResult:
    """What one step did. The pipeline run's ``detail.steps`` is a list of these."""

    id: str
    job: str
    status: str
    """``succeeded``, ``partial``, ``failed`` or ``skipped``."""
    run_id: str | None = None
    duration_seconds: float | None = None
    error: str | None = None
    counts: Mapping[str, int] = field(default_factory=dict)
    attempts: int = 0
    """How many times the step was run. Above 1 means a transient failure was retried."""
    error_class: str | None = None
    """Why it was or was not retried: ``transient``, ``rate_limited``, ``permanent``."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job": self.job,
            "status": self.status,
            "run_id": self.run_id,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "counts": dict(self.counts),
            "attempts": self.attempts,
            "error_class": self.error_class,
        }


SKIPPED = "skipped"

# A step's outcome, and what it contributes to the pipeline's own (§6.4). A
# failure the spec says to survive counts as `partial`: the pipeline did not
# do everything, but it was not stopped either.
_CONTRIBUTION: dict[tuple[Outcome, str], Outcome] = {
    (Outcome.FAILED, "stop"): Outcome.FAILED,
    (Outcome.FAILED, "continue"): Outcome.PARTIAL,
    (Outcome.FAILED, "skip_rest"): Outcome.PARTIAL,
}


def step_contribution(outcome: Outcome, on_failure: OnFailure) -> Outcome:
    """What a finished step contributes to the pipeline's outcome."""
    return _CONTRIBUTION.get((outcome, on_failure), outcome)


def continues_after(outcome: Outcome, step: StepSpec) -> bool:
    """Whether the steps after this one still run."""
    if outcome is Outcome.FAILED:
        return step.on_failure == "continue"
    if outcome is Outcome.PARTIAL:
        return step.on_partial == "continue"
    return True


def pipeline_outcome(contributions: Sequence[Outcome]) -> Outcome:
    """The worst of what the steps that ran contributed."""
    if not contributions:
        return Outcome.SUCCEEDED
    return max(contributions, key=lambda outcome: _SEVERITY[outcome])


_STAGE_STATUS = {
    Outcome.SUCCEEDED: "ok",
    Outcome.PARTIAL: "partial",
    Outcome.FAILED: "error",
    Outcome.RUNNING: "error",
}


def pipeline_job(spec: PipelineSpec) -> Job:
    """The :class:`Job` that runs ``spec``, for ``run_job`` to execute.

    A pipeline is not in ``JOBS``: it is defined by a file, not by the
    registry. Everything else about it is an ordinary job, which is why the
    parent ledger row, the ``run=`` log prefix, the stage fan-out and the
    "never raises" rule all come for free.
    """
    writes = frozenset({"ledger"}).union(*(JOBS[step.job].writes for step in spec.steps))

    def body(ctx: JobContext) -> RunOutcome:
        return _run_steps(spec, ctx)

    return Job(
        name=spec.name,
        run=body,
        schedule=NEVER,
        # The agents process is where non-ingest jobs live; the worker keeps
        # ingest to itself (design §7.1, §8.1).
        role="agents",
        writes=writes,
        description=spec.description or f"pipeline: {', '.join(step.id for step in spec.steps)}",
        ledger=run_ledger,
        # Every step is idempotent — the loader refuses a spec where one is
        # not — so the pipeline is too.
        idempotent=True,
    )


def _run_steps(spec: PipelineSpec, ctx: JobContext) -> RunOutcome:
    """Run the steps in order, obeying §6.4. The body of the pipeline job."""
    sleep = ctx.params.get("sleep") or time.sleep
    jitter = ctx.params.get("jitter") or random.uniform
    clock = ctx.params.get("clock") or now_utc
    # From when the steps start, on the same clock the checks below read.
    # Taking it from `ctx.now` would mix two time sources, and a test that
    # injects a clock would measure the gap between them.
    expires_at = clock() + timedelta(seconds=spec.deadline) if spec.deadline else None

    results: list[StepResult] = []
    contributions: list[Outcome] = []
    stopped_by: str | None = None

    for step in spec.steps:
        if stopped_by is None and expires_at is not None and clock() >= expires_at:
            # The deadline is the pipeline's, checked between steps: no step
            # is interrupted, and the ones that have not started do not.
            stopped_by = "deadline"
            contributions.append(Outcome.FAILED)
        if stopped_by is not None:
            results.append(StepResult(id=step.id, job=step.job, status=SKIPPED))
            _stage(ctx, step, SKIPPED, {"job": step.job, "after": stopped_by})
            continue

        child, attempts, error_class = _run_with_retries(
            spec, step, ctx, expires_at=expires_at, sleep=sleep, jitter=jitter, clock=clock
        )
        outcome = child.outcome
        results.append(
            StepResult(
                id=step.id,
                job=step.job,
                status=outcome.value,
                run_id=child.run_id,
                duration_seconds=child.duration_seconds,
                error=(child.result.errors.get("__run__") if child.result else None),
                counts=(child.result.counts if child.result else {}),
                attempts=attempts,
                error_class=error_class.value if error_class else None,
            )
        )
        contributions.append(step_contribution(outcome, step.on_failure))
        detail: dict[str, Any] = {"job": step.job, "run": child.run_id[:8]}
        if attempts > 1:
            detail["attempts"] = attempts
        _stage(ctx, step, _STAGE_STATUS[outcome], detail)
        if not continues_after(outcome, step):
            stopped_by = step.id

    outcome = pipeline_outcome(contributions)
    service_logger.info(
        "Pipeline %s: %s — %s",
        spec.name,
        outcome.value,
        ", ".join(f"{result.id}={result.status}" for result in results) or "no steps",
    )
    errors: dict[str, str] = {}
    for result in results:
        if result.error is not None:
            errors[result.id] = result.error
    return RunOutcome(
        outcome=outcome,
        counts=_counts(results),
        errors=errors,
        detail={
            "pipeline": spec.name,
            "version": spec.version,
            # One moment for the whole run (§6.3). Stored as a string because
            # the detail column is JSONB.
            "as_of": ctx.now.isoformat(),
            # The record of what each step did, skipped ones included: "what
            # did not run" must never be an absence. `detail.stages`, which the
            # ledger hook writes, is the compact version the UI strip draws.
            "steps": [result.to_dict() for result in results],
            **({"stopped_by": stopped_by} if stopped_by else {}),
        },
    )


def _run_with_retries(
    spec: PipelineSpec,
    step: StepSpec,
    ctx: JobContext,
    *,
    expires_at: datetime | None,
    sleep: Callable[[float], None],
    jitter: Callable[[float, float], float],
    clock: Callable[[], datetime],
) -> tuple[Run, int, ErrorClass | None]:
    """Run one step, trying again while the failure is worth trying again.

    **Only a step that raised is retried**, and only when
    :func:`harness.errors.classify` says another attempt could help. A job
    that *returned* ``failed`` has graded itself — the news fetch does this
    for a rate limit it already backed off from — and running it again would
    overrule a decision the job was in a better position to make.

    Every attempt is its own ``run_job`` call, so every attempt is its own
    ledger row with its own ``parent_run_id``; the step's record says how
    many there were. Backoff is exponential with **full jitter**: without the
    jitter, several steps that failed together retry together and fail
    together again.

    Returns:
        The last attempt's run, how many attempts there were, and the class
        of the final error (``None`` when it did not fail).
    """
    retry = spec.retry_for(step)
    timeout = spec.timeout_for(step)
    job = JOBS[step.job]
    params = step_params(step)
    error_class: ErrorClass | None = None

    for attempt in range(1, retry.attempts + 1):
        deadline = _step_deadline(timeout, expires_at, clock)
        child = run_job(
            job,
            ctx.settings,
            # Not "manual": a step ran because the pipeline did, and the
            # pipeline knows why it did.
            trigger=ctx.run.trigger,
            params={"as_of": ctx.now, "deadline": deadline, "attempt": attempt, **params},
            parent_run_id=ctx.run.run_id,
            db=ctx.db,
        )
        if child.outcome is not Outcome.FAILED or child.exception is None:
            return child, attempt, None
        error_class = classify(child.exception)
        if not error_class.retryable or attempt == retry.attempts:
            return child, attempt, error_class

        delay = jitter(0.0, min(retry.max_backoff, retry.backoff * (2 ** (attempt - 1))))
        if expires_at is not None and clock() + timedelta(seconds=delay) >= expires_at:
            # Sleeping past the deadline only delays the same failure.
            return child, attempt, error_class
        service_logger.warning(
            "Pipeline %s step %s failed (%s); attempt %d of %d in %.1fs",
            spec.name,
            step.id,
            error_class.value,
            attempt,
            retry.attempts,
            delay,
        )
        sleep(delay)

    raise AssertionError("unreachable: the loop returns on the last attempt")  # pragma: no cover


def step_params(step: StepSpec) -> dict[str, Any]:
    """The step's params as its job's model defines them, not as they were typed.

    The loader validates these too, but for the *message* — it needs the line
    number. This is the call whose **result** is used, because a validated
    value is often not the written one: ``strategies: all`` is the empty
    tuple that means "every registered strategy", and handing the body the
    raw string instead gives it three one-letter strategy names. Doing it
    here rather than at load time also covers a spec built in Python, which
    never went through the loader at all.
    """
    job = JOBS.get(step.job)
    if job is None or job.params is None:
        return dict(step.params)
    given = {key: value for key, value in step.params.items() if key not in RESERVED_PARAMS}
    return job.params.model_validate(given).model_dump()


def _step_deadline(
    timeout: float | None, expires_at: datetime | None, clock: Callable[[], datetime]
) -> datetime | None:
    """When this attempt should stop, as the earlier of its timeout and the run's.

    Cooperative: it is passed to the job, which checks it between units of
    work. A job that does not check simply runs on — Python cannot safely
    kill a thread, and pretending otherwise would be worse than saying so
    (pipelines DESIGN §7.5).
    """
    own = clock() + timedelta(seconds=timeout) if timeout is not None else None
    candidates = [moment for moment in (own, expires_at) if moment is not None]
    return min(candidates) if candidates else None


def _counts(results: Sequence[StepResult]) -> dict[str, int]:
    counts = {"steps": len(results)}
    for status in (Outcome.SUCCEEDED.value, Outcome.PARTIAL.value, Outcome.FAILED.value, SKIPPED):
        total = sum(1 for result in results if result.status == status)
        if total:
            counts[status] = total
    return counts


def _stage(ctx: JobContext, step: StepSpec, status: str, detail: dict[str, Any]) -> None:
    if ctx.on_stage is None:  # pragma: no cover - run_job always supplies one
        return
    ctx.on_stage(
        StageRecord(
            name=step.id,
            status=status,
            started_at=now_utc(),
            duration_seconds=None,
            detail=detail,
        )
    )


def run_pipeline(
    spec: PipelineSpec,
    settings: Settings,
    *,
    trigger: str = MANUAL,
    db: DatabaseManager | None = None,
    on_stage: StageHook | None = None,
    parent_run_id: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> Run:
    """Run a loaded spec once and return the closed parent :class:`Run`.

    Never raises, for the same reason ``run_job`` does not: a caller on a
    scheduler must not be taken down by the work it scheduled.

    Args:
        params: Seams for tests and direct callers — ``sleep``, ``jitter``
            and ``clock``, so a retry's backoff can be asserted without
            waiting for it. Steps get their parameters from the spec.
    """
    try:
        job = pipeline_job(spec)
    except Exception as exc:  # pragma: no cover - a loaded spec cannot reach this
        # Only possible if JOBS changed under a spec that already validated.
        # Returning a closed, failed Run keeps the contract literal.
        run = Run(job=spec.name, trigger=trigger)
        run.exception = exc
        run.finished_at = run.started_at
        run.result = RunOutcome(outcome=Outcome.FAILED, errors={"__run__": f"{type(exc).__name__}: {exc}"})
        service_logger.exception("Could not build the pipeline job for %s", spec.name)
        return run
    return run_job(
        job,
        settings,
        trigger=trigger,
        db=db,
        on_stage=on_stage,
        parent_run_id=parent_run_id,
        params=params,
    )


def steps_of(run: Run) -> list[StepResult]:
    """The step results from a finished pipeline run, for a caller that prints them."""
    detail = run.result.detail if run.result else {}
    raw = detail.get("steps", [])
    return [
        StepResult(
            id=str(item["id"]),
            job=str(item["job"]),
            status=str(item["status"]),
            run_id=item.get("run_id"),
            duration_seconds=item.get("duration_seconds"),
            error=item.get("error"),
            counts=item.get("counts") or {},
            attempts=int(item.get("attempts") or 0),
            error_class=item.get("error_class"),
        )
        for item in raw
    ]
