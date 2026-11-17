"""Pipeline specs: what the loader refuses, and what the executor records.

The loader's tests all assert the **line** as well as the message. A spec is
a file a person edits by hand, so "rejected" without "where" is half a
feature, and a regression in the line map is invisible otherwise.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from candlegraph.harness.jobs import JOBS, NEVER, Job, JobContext, run_ledger
from candlegraph.harness.pipelines import (
    PipelineError,
    PipelineSpec,
    SpecProblem,
    continues_after,
    load_pipeline,
    pipeline_outcome,
    resolve_spec,
    run_pipeline,
    step_contribution,
    steps_of,
)
from candlegraph.harness.runs import MANUAL, UI, Outcome, RunOutcome
from candlegraph.settings import REPO_ROOT
from tests.conftest import FakeDatabase, make_settings

REPO_SPECS = sorted((REPO_ROOT / "pipelines").glob("*.yaml"))


def write(tmp_path: Path, text: str, name: str = "spec.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def problems(tmp_path: Path, text: str) -> list[SpecProblem]:
    """Load a spec expected to fail, and return everything it was refused for."""
    with pytest.raises(PipelineError) as caught:
        load_pipeline(write(tmp_path, text))
    return list(caught.value.problems)


def only(tmp_path: Path, text: str) -> SpecProblem:
    found = problems(tmp_path, text)
    assert len(found) == 1, [p.message for p in found]
    return found[0]


# --- fake jobs ---------------------------------------------------------------
#
# The executor is tested against jobs that do nothing but return an outcome,
# so a step's effect on the pipeline is the only thing under test. They are
# registered in JOBS for the duration of a test, because the loader refuses a
# step naming a job that is not there — which is the point of the registry.


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: int = 24


def fake_job(name: str, body: Callable[[JobContext], RunOutcome], **extra: Any) -> Job:
    return Job(
        name=name,
        run=body,
        schedule=NEVER,
        role="agents",
        writes=frozenset({"ledger"}),
        description=f"test job {name}",
        ledger=run_ledger,
        idempotent=True,
        **extra,
    )


def _ok(ctx: JobContext) -> RunOutcome:
    return RunOutcome(outcome=Outcome.SUCCEEDED, counts={"did": 1})


def _partial(ctx: JobContext) -> RunOutcome:
    return RunOutcome(outcome=Outcome.PARTIAL)


def _failed(ctx: JobContext) -> RunOutcome:
    return RunOutcome(outcome=Outcome.FAILED, errors={"__run__": "it did not work"})


def _boom(ctx: JobContext) -> RunOutcome:
    raise RuntimeError("the body exploded")


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the fake jobs for one test."""
    for name, body in (("ok", _ok), ("partial", _partial), ("failed", _failed), ("boom", _boom)):
        monkeypatch.setitem(JOBS, name, fake_job(name, body))
    monkeypatch.setitem(JOBS, "with_params", fake_job("with_params", _ok, params=_Params))


def closed(db: FakeDatabase, job: str) -> dict[str, Any]:
    """The `close_run` parameters written for one job."""
    rows = [
        params for label, params in db.executed if label == "close_run" and params and params["job"] == job
    ]
    assert rows, f"no run row closed for {job}"
    return rows[-1]


def detail_of(db: FakeDatabase, job: str) -> dict[str, Any]:
    return dict(json.loads(closed(db, job)["detail"]))


# --- what loads --------------------------------------------------------------


def test_a_minimal_spec_loads_with_the_documented_defaults(tmp_path: Path) -> None:
    spec = load_pipeline(
        write(
            tmp_path,
            """
            name: tiny
            steps:
              - id: observe
                job: observer
            """,
        )
    )

    assert spec.name == "tiny"
    assert spec.version == 1
    assert spec.concurrency == "one_at_a_time"
    assert len(spec.steps) == 1
    # The defaults that matter are the failure rules: bad data must not flow on.
    assert spec.steps[0].on_failure == "stop"
    assert spec.steps[0].on_partial == "continue"


def test_durations_are_read_as_seconds(tmp_path: Path) -> None:
    spec = load_pipeline(
        write(
            tmp_path,
            """
            name: timed
            deadline: 15m
            defaults:
              timeout: 90s
              retry: { attempts: 3, backoff: 30s, max_backoff: 2h }
            steps:
              - id: observe
                job: observer
                timeout: 2m
            """,
        )
    )

    assert spec.deadline == 900.0
    assert spec.defaults.timeout == 90.0
    assert spec.defaults.retry.max_backoff == 7200.0
    assert spec.timeout_for(spec.steps[0]) == 120.0
    assert spec.retry_for(spec.steps[0]).attempts == 3


def test_a_step_inherits_the_file_default_timeout(tmp_path: Path) -> None:
    spec = load_pipeline(
        write(
            tmp_path,
            """
            name: timed
            defaults: { timeout: 45s }
            steps:
              - id: observe
                job: observer
            """,
        )
    )

    assert spec.timeout_for(spec.steps[0]) == 45.0


@pytest.mark.parametrize("path", REPO_SPECS, ids=lambda p: p.name)
def test_every_committed_spec_is_valid(path: Path) -> None:
    """`candlegraph run --check pipelines/*.yaml`, as a test.

    This is the gate the design asks CI for (§5.2): a spec with a typo must
    fail the build, not the first run after a deploy.
    """
    assert load_pipeline(path).steps


def test_there_is_at_least_one_committed_spec() -> None:
    # Otherwise the parametrised test above passes by having nothing to do.
    assert REPO_SPECS


# --- what is refused ---------------------------------------------------------


def test_an_unknown_key_is_refused_at_its_own_line(tmp_path: Path) -> None:
    problem = only(
        tmp_path,
        "name: tiny\n"  # 1
        "jub: ingest\n"  # 2
        "steps:\n"  # 3
        "  - id: observe\n"  # 4
        "    job: observer\n",  # 5
    )

    assert problem.line == 2
    assert "unknown key 'jub'" in problem.message


def test_an_unknown_key_inside_a_step_is_refused_at_its_own_line(tmp_path: Path) -> None:
    problem = only(
        tmp_path,
        "name: tiny\n"
        "steps:\n"
        "  - id: observe\n"
        "    job: observer\n"
        "    on_fail: stop\n",  # 5 — the real key is on_failure
    )

    assert problem.line == 5
    assert "unknown key 'on_fail'" in problem.message


def test_an_unregistered_job_is_refused_and_the_registry_is_listed(tmp_path: Path) -> None:
    problem = only(
        tmp_path,
        "name: tiny\nsteps:\n  - id: one\n    job: nosuch\n",
    )

    assert problem.line == 4
    assert "unknown job 'nosuch'" in problem.message
    assert "observer" in problem.message


def test_a_step_that_writes_market_data_is_refused(tmp_path: Path) -> None:
    """Invariant 21, checked at load time rather than hoped for at run time.

    A pipeline reacts to ingest; it never runs it (pipelines DESIGN §7.1).
    """
    found = problems(tmp_path, "name: tiny\nsteps:\n  - id: one\n    job: ingest\n")

    assert any("writes market data" in p.message and p.line == 4 for p in found), found


def test_a_job_that_is_not_declared_idempotent_cannot_be_a_step(tmp_path: Path) -> None:
    found = problems(tmp_path, "name: tiny\nsteps:\n  - id: one\n    job: janitor\n")

    assert any("not declared idempotent" in p.message and p.line == 4 for p in found), found


def test_two_steps_may_not_share_an_id(tmp_path: Path) -> None:
    problem = only(
        tmp_path,
        "name: tiny\n"
        "steps:\n"
        "  - id: observe\n"
        "    job: observer\n"
        "  - id: observe\n"  # 5
        "    job: observer\n",
    )

    assert problem.line == 5
    assert "duplicate step id 'observe'" in problem.message


def test_a_pipeline_may_not_take_a_registered_job_s_name(tmp_path: Path) -> None:
    """Both would appear in run_log's `job` column, and `runs --job` would be ambiguous."""
    problem = only(tmp_path, "name: observer\nsteps:\n  - id: one\n    job: observer\n")

    assert problem.line == 1
    assert "registered job" in problem.message


def test_an_empty_step_list_is_refused(tmp_path: Path) -> None:
    problem = only(tmp_path, "name: tiny\nsteps: []\n")

    assert problem.line == 2


def test_a_spec_with_no_steps_key_is_refused(tmp_path: Path) -> None:
    problem = only(tmp_path, "name: tiny\n")

    assert "steps" in problem.message


def test_an_empty_file_is_refused_rather_than_loaded_as_nothing(tmp_path: Path) -> None:
    problem = only(tmp_path, "")

    assert "empty" in problem.message


def test_a_file_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    problem = only(tmp_path, "- just\n- a list\n")

    assert "must be a mapping" in problem.message


def test_broken_yaml_is_refused_with_the_line_the_parser_stopped_on(tmp_path: Path) -> None:
    problem = only(tmp_path, "name: tiny\nsteps:\n  - id: one\n   job: observer\n")

    assert problem.line is not None
    assert "not valid YAML" in problem.message


def test_a_missing_file_is_refused_rather_than_treated_as_empty(tmp_path: Path) -> None:
    with pytest.raises(PipelineError) as caught:
        load_pipeline(tmp_path / "absent.yaml")

    assert "cannot read the spec" in str(caught.value)


def test_an_unknown_model_role_is_refused(tmp_path: Path) -> None:
    problem = only(
        tmp_path,
        "name: tiny\nsteps:\n  - id: one\n    job: observer\n    model_role: wizard\n",
    )

    assert problem.line == 5
    assert "unknown model_role 'wizard'" in problem.message


def test_an_event_trigger_naming_this_pipeline_is_a_loop_and_is_refused(tmp_path: Path) -> None:
    problem = only(
        tmp_path,
        "name: tiny\n"
        "triggers:\n"
        "  - event: { job: tiny }\n"  # 3
        "steps:\n"
        "  - id: one\n"
        "    job: observer\n",
    )

    assert problem.line == 3
    assert "start the next one" in problem.message


def test_a_trigger_must_be_exactly_one_of_event_or_schedule(tmp_path: Path) -> None:
    found = problems(
        tmp_path,
        "name: tiny\ntriggers:\n  - {}\nsteps:\n  - id: one\n    job: observer\n",
    )

    assert any("exactly one" in p.message for p in found), found


def test_a_deadline_shorter_than_the_step_timeouts_is_refused(tmp_path: Path) -> None:
    problem = only(
        tmp_path,
        "name: tiny\n"
        "deadline: 1m\n"  # 2
        "steps:\n"
        "  - id: one\n"
        "    job: observer\n"
        "    timeout: 5m\n",
    )

    assert problem.line == 2
    assert "would always fire" in problem.message


def test_a_deadline_longer_than_the_step_timeouts_is_fine(tmp_path: Path) -> None:
    spec = load_pipeline(
        write(
            tmp_path,
            "name: tiny\ndeadline: 10m\nsteps:\n  - id: one\n    job: observer\n    timeout: 5m\n",
        )
    )

    assert spec.deadline == 600.0


def test_a_duration_in_the_wrong_shape_is_refused(tmp_path: Path) -> None:
    problem = only(
        tmp_path,
        "name: tiny\ndeadline: soon\nsteps:\n  - id: one\n    job: observer\n",
    )

    assert problem.line == 2
    assert "duration" in problem.message


def test_a_step_may_not_set_the_as_of_the_pipeline_fixes(tmp_path: Path) -> None:
    """One run sees one moment (design §6.3), so a step cannot move it."""
    problem = only(
        tmp_path,
        "name: tiny\nsteps:\n  - id: one\n    job: observer\n    params:\n      as_of: 2020-01-01\n",  # 6
    )

    assert problem.line == 6
    assert "set by the pipeline" in problem.message


def test_params_given_to_a_job_that_takes_none_are_refused(tmp_path: Path) -> None:
    """Failing closed: a job gains params with a model, never by being passed some."""
    problem = only(
        tmp_path,
        "name: tiny\nsteps:\n  - id: one\n    job: observer\n    params: { window: 24 }\n",
    )

    assert problem.line == 5
    assert "takes no parameters" in problem.message


def test_params_are_checked_against_the_job_s_own_model(tmp_path: Path, jobs: None) -> None:
    problem = only(
        tmp_path,
        "name: tiny\nsteps:\n  - id: one\n    job: with_params\n    params:\n      windwo: 24\n",  # 6
    )

    assert problem.line == 6
    assert "unknown parameter 'windwo'" in problem.message


def test_a_param_of_the_wrong_type_is_refused(tmp_path: Path, jobs: None) -> None:
    problem = only(
        tmp_path,
        "name: tiny\nsteps:\n  - id: one\n    job: with_params\n    params:\n      window: wide\n",
    )

    assert problem.line == 6
    assert "with_params" in problem.message


def test_a_param_that_matches_the_model_loads(tmp_path: Path, jobs: None) -> None:
    spec = load_pipeline(
        write(
            tmp_path,
            "name: tiny\nsteps:\n  - id: one\n    job: with_params\n    params: { window: 48 }\n",
        )
    )

    assert spec.steps[0].params == {"window": 48}


def test_every_problem_in_a_file_is_reported_at_once(tmp_path: Path) -> None:
    """One `--check` should be enough to see every mistake, not the first."""
    found = problems(
        tmp_path,
        "name: tiny\nsteps:\n  - id: one\n    job: nosuch\n  - id: one\n    job: alsonot\n",
    )

    assert len(found) >= 3
    assert {p.line for p in found} >= {4, 5, 6}


def test_the_message_names_the_file_and_the_line(tmp_path: Path) -> None:
    path = write(tmp_path, "name: tiny\nsteps:\n  - id: one\n    job: nosuch\n")

    with pytest.raises(PipelineError) as caught:
        load_pipeline(path)

    assert f"{path}:4:" in str(caught.value)


# --- finding a spec ----------------------------------------------------------


def test_a_spec_is_found_by_name_in_the_pipelines_directory(tmp_path: Path) -> None:
    (tmp_path / "hourly.yaml").write_text("name: hourly\n", encoding="utf-8")
    settings = make_settings(pipelines_dir=str(tmp_path))

    assert resolve_spec("hourly", settings) == tmp_path / "hourly.yaml"


def test_a_path_is_used_as_written(tmp_path: Path) -> None:
    path = write(tmp_path, "name: tiny\n", name="odd.yaml")
    settings = make_settings(pipelines_dir=str(tmp_path / "elsewhere"))

    assert resolve_spec(str(path), settings) == path


def test_an_unknown_name_resolves_to_where_it_would_have_been(tmp_path: Path) -> None:
    """So the error says "cannot read .../pipelines/ghost.yaml", not "no such name"."""
    settings = make_settings(pipelines_dir=str(tmp_path))

    assert resolve_spec("ghost", settings) == tmp_path / "ghost.yaml"


# --- the outcome rules (design §6.4) -----------------------------------------


@pytest.mark.parametrize(
    ("outcome", "on_failure", "expected"),
    [
        (Outcome.SUCCEEDED, "stop", Outcome.SUCCEEDED),
        (Outcome.PARTIAL, "stop", Outcome.PARTIAL),
        (Outcome.FAILED, "stop", Outcome.FAILED),
        # A failure the spec said to survive is not a failed pipeline: the run
        # did less than it meant to, which is exactly `partial`.
        (Outcome.FAILED, "continue", Outcome.PARTIAL),
        (Outcome.FAILED, "skip_rest", Outcome.PARTIAL),
    ],
)
def test_what_a_step_contributes_to_the_pipeline_s_outcome(
    outcome: Outcome, on_failure: Any, expected: Outcome
) -> None:
    assert step_contribution(outcome, on_failure) is expected


def test_the_pipeline_takes_the_worst_outcome_of_the_steps_that_ran() -> None:
    assert pipeline_outcome([Outcome.SUCCEEDED, Outcome.PARTIAL, Outcome.SUCCEEDED]) is Outcome.PARTIAL
    assert pipeline_outcome([Outcome.SUCCEEDED, Outcome.FAILED]) is Outcome.FAILED
    assert pipeline_outcome([Outcome.SUCCEEDED]) is Outcome.SUCCEEDED
    assert pipeline_outcome([]) is Outcome.SUCCEEDED


def _step(**kwargs: Any) -> Any:
    spec = PipelineSpec.model_validate({"name": "x", "steps": [{"id": "one", "job": "observer", **kwargs}]})
    return spec.steps[0]


def test_a_failure_stops_the_rest_by_default_and_a_partial_does_not() -> None:
    assert continues_after(Outcome.FAILED, _step()) is False
    assert continues_after(Outcome.PARTIAL, _step()) is True
    assert continues_after(Outcome.SUCCEEDED, _step()) is True


def test_each_rule_can_be_turned_around_per_step() -> None:
    assert continues_after(Outcome.FAILED, _step(on_failure="continue")) is True
    assert continues_after(Outcome.FAILED, _step(on_failure="skip_rest")) is False
    assert continues_after(Outcome.PARTIAL, _step(on_partial="stop")) is False


# --- running -----------------------------------------------------------------


def spec_of(*steps: str, **extra: Any) -> PipelineSpec:
    return PipelineSpec.model_validate(
        {
            "name": "test_pipeline",
            "steps": [{"id": f"s{i}", "job": job, **extra} for i, job in enumerate(steps)],
        }
    )


def test_a_pipeline_run_owns_a_child_run_for_each_step(jobs: None) -> None:
    db = FakeDatabase()

    run = run_pipeline(spec_of("ok", "ok"), make_settings(), db=db)

    assert run.outcome is Outcome.SUCCEEDED
    opened = [params for label, params in db.executed if label == "open_run"]
    assert [params["job"] for params in opened] == ["test_pipeline", "ok", "ok"]
    # The parent owns them; the children do not own each other.
    assert opened[0]["parent_run_id"] is None
    assert {params["parent_run_id"] for params in opened[1:]} == {run.run_id}


def test_a_step_records_why_the_pipeline_ran_not_why_it_itself_was_called(jobs: None) -> None:
    """A step ran because the pipeline did, and the pipeline knows what started it."""
    db = FakeDatabase()

    run_pipeline(spec_of("ok"), make_settings(), db=db, trigger=UI)

    assert {params["trigger"] for label, params in db.executed if label == "open_run"} == {UI}


def test_the_pipeline_run_records_what_each_step_did(jobs: None) -> None:
    db = FakeDatabase()

    run = run_pipeline(spec_of("ok", "partial"), make_settings(), db=db)

    detail = detail_of(db, "test_pipeline")
    assert detail["pipeline"] == "test_pipeline"
    assert detail["version"] == 1
    assert [step["status"] for step in detail["steps"]] == ["succeeded", "partial"]
    assert [step["id"] for step in detail["steps"]] == ["s0", "s1"]
    # Each step points at its own run, so the family is navigable from the parent.
    assert all(step["run_id"] for step in detail["steps"])
    assert run.outcome is Outcome.PARTIAL


def test_every_step_of_one_run_sees_the_same_moment(jobs: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Design §6.3: the as-of time is fixed when the run opens, not per step."""
    seen: list[Any] = []

    def record(ctx: JobContext) -> RunOutcome:
        seen.append(ctx.params["as_of"])
        return RunOutcome(outcome=Outcome.SUCCEEDED)

    monkeypatch.setitem(JOBS, "recorder", fake_job("recorder", record))

    run = run_pipeline(spec_of("recorder", "recorder"), make_settings(), db=FakeDatabase())

    assert len(seen) == 2
    assert seen[0] == seen[1] == run.started_at


def test_a_failed_step_stops_the_ones_behind_it(jobs: None) -> None:
    db = FakeDatabase()

    run = run_pipeline(spec_of("failed", "ok"), make_settings(), db=db)

    assert run.outcome is Outcome.FAILED
    assert [params["job"] for label, params in db.executed if label == "open_run"] == [
        "test_pipeline",
        "failed",
    ]


def test_a_step_that_never_ran_is_recorded_as_skipped_rather_than_left_out(jobs: None) -> None:
    """ "What did not run" must be a row, not an absence."""
    db = FakeDatabase()

    run_pipeline(spec_of("failed", "ok", "ok"), make_settings(), db=db)

    detail = detail_of(db, "test_pipeline")
    assert [step["status"] for step in detail["steps"]] == ["failed", "skipped", "skipped"]
    assert detail["stopped_by"] == "s0"


def test_a_failure_the_spec_says_to_survive_leaves_the_pipeline_partial(jobs: None) -> None:
    db = FakeDatabase()

    run = run_pipeline(spec_of("failed", "ok", on_failure="continue"), make_settings(), db=db)

    assert run.outcome is Outcome.PARTIAL
    assert [params["job"] for label, params in db.executed if label == "open_run"] == [
        "test_pipeline",
        "failed",
        "ok",
    ]


def test_a_step_whose_body_raises_fails_the_step_and_not_the_process(jobs: None) -> None:
    """run_pipeline never raises, for the same reason run_job does not."""
    db = FakeDatabase()

    run = run_pipeline(spec_of("boom", "ok"), make_settings(), db=db)

    assert run.outcome is Outcome.FAILED
    assert run.exception is None  # the step failed; the pipeline completed
    steps = steps_of(run)
    assert steps[0].status == "failed"
    assert steps[0].error is not None and "the body exploded" in steps[0].error
    assert steps[1].status == "skipped"


def test_the_pipeline_s_errors_are_keyed_by_step_id(jobs: None) -> None:
    db = FakeDatabase()

    run_pipeline(spec_of("failed"), make_settings(), db=db)

    errors = json.loads(closed(db, "test_pipeline")["errors"])
    assert errors == {"s0": "it did not work"}


def test_each_step_is_a_stage_so_the_dashboard_strip_draws_a_pipeline_run(jobs: None) -> None:
    db = FakeDatabase()

    run_pipeline(spec_of("ok", "partial", "failed"), make_settings(), db=db)

    stages = detail_of(db, "test_pipeline")["stages"]
    assert [stage["name"] for stage in stages] == ["s0", "s1", "s2"]
    # A step that half-worked must not display as "ok".
    assert [stage["status"] for stage in stages] == ["ok", "partial", "error"]


def test_the_steps_are_announced_to_a_caller_watching_progress(jobs: None) -> None:
    seen: list[str] = []

    run_pipeline(
        spec_of("ok", "ok"),
        make_settings(),
        db=FakeDatabase(),
        on_stage=lambda record: seen.append(record.name),
    )

    assert seen == ["s0", "s1"]


def test_the_counts_say_how_many_steps_did_what(jobs: None) -> None:
    db = FakeDatabase()

    run_pipeline(spec_of("ok", "partial", "failed", "ok"), make_settings(), db=db)

    counts = json.loads(closed(db, "test_pipeline")["counts"])
    assert counts == {"steps": 4, "succeeded": 1, "partial": 1, "failed": 1, "skipped": 1}


def test_a_pipeline_job_is_never_scheduled(jobs: None) -> None:
    """It runs when the pipeline runs; the pipeline carries the schedule."""
    from candlegraph.harness.pipelines import pipeline_job

    job = pipeline_job(spec_of("ok"))

    assert job.schedule.seconds(make_settings()) == 0
    assert job.idempotent is True
    assert "market_data" not in job.writes


def test_a_pipeline_defaults_to_a_manual_trigger(jobs: None) -> None:
    db = FakeDatabase()

    run_pipeline(spec_of("ok"), make_settings(), db=db)

    assert closed(db, "test_pipeline")["trigger"] == MANUAL


def test_the_committed_example_runs_and_records_both_rows() -> None:
    """The example the README and the docs tell people to run, end to end.

    The observer's `agent_run` row is written only when the configured sinks
    include `ledger` — the default, and what the stack runs with. If that ever
    stops being true, a pipeline run would show a parent with no children, so
    the assertion is on the child row rather than on the step's outcome.
    """
    spec = load_pipeline(REPO_ROOT / "pipelines" / "example.yaml")
    db = FakeDatabase()

    run = run_pipeline(spec, make_settings(agent_report_sinks="log,ledger"), db=db)

    assert run.job == "example"
    assert [step.job for step in steps_of(run)] == ["observer"]
    opened = [label for label, _ in db.executed]
    assert "open_run" in opened  # the pipeline, in the generic ledger
    assert "open_agent_run" in opened  # the observer, in its own
    agent_rows = [params for label, params in db.executed if label == "open_agent_run" and params]
    assert agent_rows[0]["parent_run_id"] == run.run_id


def test_a_pipeline_whose_steps_all_succeed_is_not_partial(jobs: None) -> None:
    run = run_pipeline(spec_of("ok", "ok", "ok"), make_settings(), db=FakeDatabase())

    assert run.outcome is Outcome.SUCCEEDED
    assert run.duration_seconds is not None


def test_a_job_removed_from_the_registry_after_loading_fails_the_run_rather_than_the_caller(
    jobs: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one way pipeline_job can fail: the registry moved under a loaded spec."""
    spec = spec_of("ok")
    monkeypatch.delitem(JOBS, "ok")

    run = run_pipeline(spec, make_settings(), db=FakeDatabase())

    assert run.outcome is Outcome.FAILED
    assert run.exception is not None


def test_a_spec_is_frozen_once_loaded(tmp_path: Path) -> None:
    """Nothing may edit a spec between validating it and running it."""
    spec = load_pipeline(write(tmp_path, "name: tiny\nsteps:\n  - id: one\n    job: observer\n"))

    with pytest.raises(ValidationError):
        spec.name = "other"  # type: ignore[misc]
