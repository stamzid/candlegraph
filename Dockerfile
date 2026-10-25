# syntax=docker/dockerfile:1

# uv is pinned by digest-bearing tag so the image is reproducible; an unpinned
# installer is what broke this build when Poetry went 2.x.
FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

FROM python:3.12-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /candlegraph

# Dependencies first: this layer is cached until the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev --extra ui

# Then the project itself.
COPY README.md ./
COPY candlegraph ./candlegraph
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra ui

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/candlegraph/.venv/bin:$PATH"
WORKDIR /candlegraph

RUN useradd --create-home --uid 10001 candlegraph

COPY --from=builder --chown=candlegraph:candlegraph /candlegraph/.venv /candlegraph/.venv
COPY --chown=candlegraph:candlegraph candlegraph ./candlegraph
COPY --chown=candlegraph:candlegraph alembic ./alembic
COPY --chown=candlegraph:candlegraph config ./config
COPY --chown=candlegraph:candlegraph alembic.ini pyproject.toml README.md entrypoint.sh ./

RUN chmod +x ./entrypoint.sh
USER candlegraph
EXPOSE 8000 8501

ENTRYPOINT ["./entrypoint.sh"]
CMD ["python", "-m", "candlegraph"]
