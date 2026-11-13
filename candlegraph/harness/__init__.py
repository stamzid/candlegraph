"""The runner: one way to schedule, run and record a job.

``jobs.JOBS`` is the registry, ``runs.run_job`` the one entry point every run
passes through, ``scheduler.build_scheduler`` the one APScheduler builder and
``process.run_process`` the one foreground loop. The ``worker`` and ``agents``
commands are two *selections* of jobs over this code, and the API's in-process
ingest is a third. See ``docs/harness/runner/DESIGN.md``.

Importing this package connects to nothing and imports no job body
(invariant 1): the bodies are imported inside the job functions, so
``candlegraph serve`` never pulls LangGraph in through the registry.
"""

from candlegraph.harness.jobs import JOBS, Every, Job, JobContext, select
from candlegraph.harness.runs import Outcome, Run, RunOutcome, run_job

__all__ = ["JOBS", "Every", "Job", "JobContext", "Outcome", "Run", "RunOutcome", "run_job", "select"]
