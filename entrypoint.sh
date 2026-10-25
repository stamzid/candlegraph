#!/bin/sh
# Apply migrations, then hand off to the command (the API server by default).
# Failing here is correct: serving against an unmigrated schema is worse than
# not starting.
set -eu

echo "Applying database migrations..."
alembic upgrade head

exec "$@"
