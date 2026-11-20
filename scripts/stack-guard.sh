#!/bin/sh
# Refuse to act on a Compose project that belongs to a different checkout.
#
#   scripts/stack-guard.sh up|down|reset   exit 1 if this project is another checkout's
#   scripts/stack-guard.sh list            every candlegraph stack on this daemon, and whose it is
#
# Why this exists (docs/incidents/2026-11-20-stack-would-not-start-again/):
# a Compose project name is the only thing that separates two checkouts'
# stacks, and it is easy to reuse — two worktrees with one basename, or a
# name copied from another session. When that happens, `make up` replaces
# the other checkout's containers with this checkout's image and runs this
# checkout's migrations against *its* database, and `make down` deletes its
# volume. Both have happened. The failure surfaces later, somewhere else, as
# "the database is stamped at revision <hash>, which does not exist in this
# checkout".
#
# The owner is read from what Compose already records on every container,
# `com.docker.compose.project.working_dir`. Nothing is added to the compose
# file: labelling the volume there was tried and rejected, because Compose
# then treats every existing volume as changed and stops to ask whether to
# recreate it — that is, to delete the database.
#
# FORCE=1 skips the refusal, for when the other checkout is gone or you mean
# to take its stack over. Never reached for by default.
set -eu

action=${1:-}
case "$action" in
  up | down | reset | list) ;;
  *)
    echo "usage: $0 up|down|reset|list" >&2
    exit 2
    ;;
esac

here=$(pwd -P)

# The path a container recorded, made comparable: resolved if it still
# exists, as written if it does not.
resolve() {
  if [ -d "$1" ]; then
    (cd "$1" && pwd -P)
  else
    printf '%s\n' "$1"
  fi
}

if [ "$action" = list ]; then
  printf '%-34s %-28s %s\n' "project" "api container" "checkout"
  # One API container per stack, so one line per stack.
  docker ps -a --filter "label=com.docker.compose.service=candlegraph" \
    --format '{{.Label "com.docker.compose.project"}}|{{.Status}}|{{.Label "com.docker.compose.project.working_dir"}}' |
    sort -u |
    while IFS='|' read -r project status owner; do
      mark=""
      [ "$(resolve "$owner")" = "$here" ] && mark="  <- this checkout"
      [ -d "$owner" ] || mark="  (checkout no longer exists)"
      printf '%-34s %-28s %s%s\n' "$project" "$(printf '%s' "$status" | cut -c1-28)" "$owner" "$mark"
    done
  echo
  echo "Volumes with no containers left cannot be attributed to a checkout:"
  docker volume ls --format '{{.Name}}' | grep '_pgdata$' | while read -r volume; do
    project=${volume%_pgdata}
    if [ -z "$(docker ps -aq --filter "label=com.docker.compose.project=$project")" ]; then
      echo "  $volume"
    fi
  done
  exit 0
fi

# A draft migration applied to the long-lived default stack is the fault that
# has made a volume unstartable twice on record (2026-10-28, `d2f8b6a41c35`;
# 2026-11-19, `9b3d7e21c4f5`): the draft is renamed before it is committed,
# and no checkout can ever reach the schema that volume now has. So `up` on
# the *default* project refuses while alembic/versions has uncommitted
# changes. An explicitly named project passes — that is where drafts belong.
if [ "$action" = up ] && [ -z "${COMPOSE_PROJECT_NAME:-}" ] && [ "${FORCE:-}" != 1 ]; then
  drafts=$(git status --porcelain -- alembic/versions 2>/dev/null | grep '\.py$' || true)
  if [ -n "$drafts" ]; then
    cat >&2 <<EOF

Refusing to 'up' this checkout's default stack: alembic/versions has uncommitted changes,
$(printf '%s\n' "$drafts" | sed 's/^/    /')
The default stack's volume is the long-lived one. Apply a draft revision to it, rename or
rewrite the draft before committing, and that volume is stamped with a revision no
checkout has — it can never start again without being rebuilt. That has happened twice.

Try the draft on a throwaway stack instead, and delete it when you are done:
    COMPOSE_PROJECT_NAME=cg-$(basename "$here")-$(date +%s) POSTGRES_HOST_PORT=5433 APP_HOST_PORT=8001 \\
    UI_HOST_PORT=8502 OLLAMA_HOST_PORT=11435 make up
Or, once the revision id is final and you mean it:
    make up FORCE=1

EOF
    exit 1
  fi
fi

# The project name Compose itself would use here: COMPOSE_PROJECT_NAME, else
# the directory. If Compose cannot answer (no daemon, a broken .env), stand
# aside and let the real command report it.
project=$(docker compose config 2>/dev/null | sed -n 's/^name: *//p' | head -n 1) || true
[ -n "$project" ] || exit 0

owners=$(docker ps -a --filter "label=com.docker.compose.project=$project" \
  --format '{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null | sort -u) || true

foreign=""
for owner in $owners; do
  if [ "$(resolve "$owner")" != "$here" ]; then
    foreign="$owner"
    break
  fi
done
[ -n "$foreign" ] || exit 0

if [ -d "$foreign" ]; then
  state="which still exists"
else
  state="which no longer exists"
fi

if [ "${FORCE:-}" = 1 ]; then
  echo "stack-guard: FORCE=1 — acting on '$project', which belongs to $foreign ($state)." >&2
  exit 0
fi

case "$action" in
  up) consequence="'up' would replace its containers with this checkout's image and run this checkout's migrations against its database." ;;
  *) consequence="'$action' would delete its database volume, and with it everything that checkout has ingested." ;;
esac

suggest="cg-$(basename "$here")-$(date +%s)"
cat >&2 <<EOF

Refusing to '$action' Compose project '$project': its containers belong to another checkout,
    $foreign
$state. $consequence

To run your own stack beside it, pick a project name only this checkout uses and free ports:
    COMPOSE_PROJECT_NAME=$suggest POSTGRES_HOST_PORT=5433 APP_HOST_PORT=8001 \\
    UI_HOST_PORT=8502 OLLAMA_HOST_PORT=11435 make $action

\`make stacks\` lists every candlegraph stack on this daemon and the checkout it belongs to.
If that checkout is gone, or you mean to take this stack over:
    make $action FORCE=1

EOF
exit 1
