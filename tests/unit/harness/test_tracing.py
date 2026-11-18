"""Spans for runs and stages, and the fact that tracing costs nothing when off.

The exported spans are checked with the SDK's in-memory exporter, which is
the same code path a collector would see — so these assert what would
actually arrive, not what the hook intended to send.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from candlegraph.harness import JOBS, Outcome, RunOutcome, run_job
from candlegraph.harness.jobs import JobContext
from candlegraph.harness.stages import StageRecord
from candlegraph.harness.tracing import RUN_JOB, RUN_OUTCOME, RUN_TRIGGER, configure_tracing, reset_tracing
from tests.conftest import FakeDatabase, make_settings


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """A real SDK writing into memory, restored afterwards.

    `trace.set_tracer_provider` refuses to replace an existing provider, so
    the module global is swapped directly — the documented escape hatch for
    tests, and the reason this fixture puts it back.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # The module global, not `get_tracer_provider()`: unset, that call returns
    # a ProxyTracerProvider which *reads* this global, so putting the proxy
    # back into it makes it delegate to itself and recurse until the stack
    # runs out. `None` is the real "nothing configured" value.
    previous = trace._TRACER_PROVIDER  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    try:
        yield exporter
    finally:
        trace._TRACER_PROVIDER = previous  # type: ignore[attr-defined]
        reset_tracing()


def body(outcome: Outcome = Outcome.SUCCEEDED, **kwargs: Any) -> Any:
    def run(ctx: JobContext) -> RunOutcome:
        if ctx.on_stage is not None:
            ctx.on_stage(StageRecord("collect", "ok", None, 0.25))
        return RunOutcome(outcome=outcome, **kwargs)

    return run


def job(name: str = "observer", **replacements: Any) -> Any:
    from dataclasses import replace

    return replace(JOBS[name], **replacements)


def names(exporter: InMemorySpanExporter) -> list[str]:
    return [span.name for span in exporter.get_finished_spans()]


def test_a_run_is_one_span_named_for_its_job(spans: InMemorySpanExporter) -> None:
    run_job(job(run=body()), make_settings(), db=FakeDatabase())

    (span,) = spans.get_finished_spans()
    assert span.name == "run observer"
    assert span.attributes[RUN_JOB] == "observer"
    assert span.attributes[RUN_TRIGGER] == "scheduled"
    assert span.attributes[RUN_OUTCOME] == "succeeded"


def test_a_stage_is_an_event_on_the_run_s_span(spans: InMemorySpanExporter) -> None:
    """An event, not a child span: a stage is reported after it has finished,
    so a span would need a start time we do not have and would have to be
    invented."""
    run_job(job(run=body()), make_settings(), db=FakeDatabase())

    (span,) = spans.get_finished_spans()
    (event,) = span.events
    assert event.name == "stage collect"
    assert event.attributes["candlegraph.stage.status"] == "ok"
    assert event.attributes["candlegraph.stage.duration_seconds"] == 0.25


def test_counts_become_attributes(spans: InMemorySpanExporter) -> None:
    run_job(job(run=body(counts={"rows": 7})), make_settings(), db=FakeDatabase())

    (span,) = spans.get_finished_spans()
    assert span.attributes["candlegraph.count.rows"] == 7


def test_a_failed_run_is_an_error_span_carrying_its_exception(spans: InMemorySpanExporter) -> None:
    def explode(ctx: JobContext) -> RunOutcome:
        raise RuntimeError("the body fell over")

    run_job(job(run=explode), make_settings(), db=FakeDatabase())

    (span,) = spans.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert span.attributes[RUN_OUTCOME] == "failed"
    assert [e.name for e in span.events] == ["exception"]


def test_a_pipeline_s_steps_are_children_of_its_span(spans: InMemorySpanExporter) -> None:
    """The shape the whole feature exists for: one trace per pipeline run.

    Nothing wires this up — the steps call `run_job` inside the pipeline's
    body on the same thread, so OpenTelemetry's context does the nesting.
    """
    from candlegraph.harness.pipelines import PipelineSpec, run_pipeline

    spec = PipelineSpec.model_validate({"name": "test_pipeline", "steps": [{"id": "a", "job": "observer"}]})

    run_pipeline(spec, make_settings(agent_report_sinks="log"), db=FakeDatabase())

    finished = spans.get_finished_spans()
    by_name = {span.name: span for span in finished}
    assert {"run observer", "run test_pipeline"} <= set(by_name)
    step, pipeline = by_name["run observer"], by_name["run test_pipeline"]
    assert step.parent is not None
    assert step.parent.span_id == pipeline.context.span_id
    # One trace, so a reader sees the pipeline and its steps together.
    assert step.context.trace_id == pipeline.context.trace_id


def test_the_step_span_closes_before_the_pipeline_s(spans: InMemorySpanExporter) -> None:
    """A step left open would make every later step a child of it."""
    from candlegraph.harness.pipelines import PipelineSpec, run_pipeline

    spec = PipelineSpec.model_validate(
        {
            "name": "test_pipeline",
            "steps": [{"id": "a", "job": "observer"}, {"id": "b", "job": "observer"}],
        }
    )

    run_pipeline(spec, make_settings(agent_report_sinks="log"), db=FakeDatabase())

    steps = [s for s in spans.get_finished_spans() if s.name == "run observer"]
    assert len(steps) == 2
    assert {s.parent.span_id for s in steps if s.parent} == {
        next(s for s in spans.get_finished_spans() if s.name == "run test_pipeline").context.span_id
    }


# --- off by default ----------------------------------------------------------


def test_with_no_sdk_configured_a_span_records_nothing() -> None:
    """Why there is no `if tracing_enabled:` anywhere.

    The API's default provider hands back a NonRecordingSpan, so the hook
    runs and costs a few no-op calls. Turning tracing on is a deployment
    decision, not a code path.
    """
    from candlegraph.harness.tracing import tracer

    with tracer().start_as_current_span("x") as span:
        assert span.is_recording() is False


def test_configuring_is_a_no_op_unless_it_is_switched_on() -> None:
    reset_tracing()
    assert configure_tracing(make_settings(otel_enabled=False)) is False


def test_the_hook_is_installed_for_every_run() -> None:
    from candlegraph.harness.hooks import default_hooks
    from candlegraph.harness.tracing import TracingHook

    assert any(isinstance(hook, TracingHook) for hook in default_hooks())


def test_each_run_gets_its_own_hook_instance() -> None:
    """One hook holds one span; sharing one would close the wrong run's."""
    from candlegraph.harness.hooks import default_hooks

    first = next(h for h in default_hooks() if type(h).__name__ == "TracingHook")
    second = next(h for h in default_hooks() if type(h).__name__ == "TracingHook")
    assert first is not second
