"""Every run is a span; every stage is a child of it.

Rollout step 6 (``docs/rollout/ROLLOUT.md``; pipelines DESIGN §8.3). The
harness already knew when a run started, what it did and how it ended — the
ledger records all of it. What the ledger cannot show is **shape**: that a
15-minute hourly pipeline was 14 minutes of `explain` waiting on a model, or
that the step which failed had already been retried twice. A trace shows that
without a join.

Three decisions worth stating.

**The instrumentation is unconditional and free.** ``opentelemetry-api`` is
an API with no implementation: with no SDK configured, ``get_tracer`` returns
a no-op tracer and every span is a ``NonRecordingSpan`` whose methods do
nothing. So there is no ``if tracing_enabled:`` anywhere — the hook is always
installed, and costs a few attribute sets on a path that already writes a
database row. Turning tracing *on* means installing the SDK (the ``otel``
extra) and pointing it at a collector; this module is unchanged either way.

**Nesting comes from the context, not from us.** A pipeline's steps call
``run_job`` inside the pipeline's own body, on the same thread, so OpenTelemetry's
context variable makes each step's span a child of the pipeline's. The
``parent_run_id`` link in the ledger and the span parentage say the same
thing by two different means, which is a useful cross-check when one of them
looks wrong.

**A failed export never fails a run.** Configuration happens once, at process
start, and anything that goes wrong there is logged and dropped — the same
rule the report sinks and the ledger hooks follow (invariant 12's spirit).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from candlegraph.harness.runs import Outcome
from candlegraph.settings import Settings
from candlegraph.utils.logging_utils import service_logger

if TYPE_CHECKING:
    from candlegraph.harness.runs import Run, RunOutcome
    from candlegraph.harness.stages import StageRecord

TRACER_NAME = "candlegraph.harness"

# Attribute names. Prefixed, because the OpenTelemetry semantic conventions
# own the unprefixed namespace and a future convention called `job` would
# silently collide with ours.
RUN_ID = "candlegraph.run.id"
RUN_JOB = "candlegraph.run.job"
RUN_TRIGGER = "candlegraph.run.trigger"
RUN_OUTCOME = "candlegraph.run.outcome"
RUN_VERDICT = "candlegraph.run.verdict"
RUN_PARENT = "candlegraph.run.parent_id"
STAGE_NAME = "candlegraph.stage.name"
STAGE_STATUS = "candlegraph.stage.status"


def tracer() -> trace.Tracer:
    """The one tracer this package reports under."""
    return trace.get_tracer(TRACER_NAME)


class TracingHook:
    """Opens a span when a run starts and closes it when the run ends.

    The span is kept on the hook rather than in a context manager because
    ``RunHook`` is three separate calls, not a block. One hook instance
    serves one run — :func:`default_hooks` builds a new one each time.
    """

    def __init__(self) -> None:
        self._span: trace.Span | None = None
        self._scope: Any = None

    def started(self, run: Run) -> None:
        span = tracer().start_span(
            f"run {run.job}",
            kind=SpanKind.INTERNAL,
            attributes={
                RUN_ID: run.run_id,
                RUN_JOB: run.job,
                RUN_TRIGGER: run.trigger,
                **({RUN_PARENT: run.parent_run_id} if run.parent_run_id else {}),
            },
        )
        self._span = span
        # Make it current, so a step's span nests under its pipeline's and
        # any library that emits spans inside the job body does too.
        self._scope = trace.use_span(span, end_on_exit=False)
        self._scope.__enter__()

    def stage(self, run: Run, record: StageRecord) -> None:
        if self._span is None:
            return
        # A stage is reported *after* it finished, so there is no window to
        # hold a span open across — an event on the run's span says the same
        # thing without inventing a duration we would have to fake.
        self._span.add_event(
            f"stage {record.name}",
            attributes={
                STAGE_NAME: record.name,
                STAGE_STATUS: record.status,
                **(
                    {"candlegraph.stage.duration_seconds": record.duration_seconds}
                    if record.duration_seconds is not None
                    else {}
                ),
            },
        )

    def finished(self, run: Run, outcome: RunOutcome) -> None:
        span, self._span = self._span, None
        if span is None:
            return
        try:
            span.set_attribute(RUN_OUTCOME, outcome.outcome.value)
            if outcome.verdict:
                span.set_attribute(RUN_VERDICT, outcome.verdict)
            for key, value in outcome.counts.items():
                span.set_attribute(f"candlegraph.count.{key}", value)
            if outcome.outcome is Outcome.FAILED:
                # The message is already redacted by run_job; a span is an
                # export target like any other (invariant 21's rule).
                span.set_status(Status(StatusCode.ERROR, outcome.errors.get("__run__")))
                if run.exception is not None:
                    span.record_exception(run.exception)
            else:
                span.set_status(Status(StatusCode.OK))
        finally:
            if self._scope is not None:
                self._scope.__exit__(None, None, None)
                self._scope = None
            span.end()


# --- turning it on ---------------------------------------------------------------

_configured = False


def configure_tracing(settings: Settings) -> bool:
    """Install the SDK and exporter, once per process. Returns whether it is on.

    Called from the process entry points. Without ``OTEL_ENABLED`` — or
    without the ``otel`` extra installed — this does nothing and the no-op
    tracer stays in place, which is the default and costs nothing.
    """
    global _configured
    if _configured or not settings.otel_enabled:
        return _configured

    try:
        from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        service_logger.warning(
            "OTEL_ENABLED is set but the SDK is missing; install it with "
            "`uv sync --extra otel`. Tracing stays off."
        )
        return False

    try:
        resource = Resource.create({SERVICE_NAME: settings.otel_service_name, SERVICE_VERSION: _version()})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(_exporter(settings)))
        trace.set_tracer_provider(provider)
    except Exception as exc:
        # A collector that is down, a bad endpoint, a missing exporter
        # package: none of it is a reason to refuse to do the work.
        service_logger.warning("Could not start tracing: %s. Carrying on without it.", exc)
        return False

    _configured = True
    service_logger.info(
        "Tracing on: %s -> %s", settings.otel_service_name, settings.otel_exporter_endpoint or "console"
    )
    return True


def _exporter(settings: Settings) -> Any:
    """OTLP over HTTP when an endpoint is configured, the console otherwise.

    The console exporter is what makes this testable by hand: `OTEL_ENABLED=true`
    with no endpoint prints the spans, which is enough to see the shape of a
    pipeline run without running a collector.
    """
    if not settings.otel_exporter_endpoint:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return ConsoleSpanExporter()
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=f"{settings.otel_exporter_endpoint.rstrip('/')}/v1/traces")


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("candlegraph")
    except PackageNotFoundError:  # pragma: no cover - only when run from a source tree
        return "unknown"


def reset_tracing() -> None:
    """Forget that configuration happened. For tests only."""
    global _configured
    _configured = False
