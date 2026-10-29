#!/bin/sh
# Apply migrations when asked, then hand off to the command (the API server
# by default). Failing here is correct: serving against an unmigrated schema
# is worse than not starting.
#
# RUN_MIGRATIONS defaults to true so a single-process deployment and a bare
# `docker run` still migrate. The compose stack sets it true for the API
# container only and false for the worker, UI and agents, which wait for the
# API to be healthy instead of each running `alembic upgrade head` themselves.
set -eu

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  echo "Applying database migrations..."
  alembic upgrade head
else
  echo "RUN_MIGRATIONS=${RUN_MIGRATIONS}: skipping migrations (another container owns them)"
fi

exec "$@"
