# Base Image
FROM python:3.11-slim as base
ENV PYTHONUNBUFFERED=true
WORKDIR /candlegraph

# Builder Stage
FROM base as builder
ENV POETRY_HOME=/opt/poetry
ENV POETRY_VIRTUALENVS_IN_PROJECT=true
ENV PATH="$POETRY_HOME/bin:$PATH"
RUN python -c 'from urllib.request import urlopen; print(urlopen("https://install.python-poetry.org").read().decode())' | python -
COPY ./pyproject.toml ./pyproject.toml
RUN poetry install --no-interaction --no-ansi -vvv

# Runtime Stage
FROM base as runtime
COPY --from=builder /candlegraph/.venv /candlegraph/.venv
ENV PATH="/candlegraph/.venv/bin:$PATH"
ENV GRPC_DNS_RESOLVER=native
COPY ./ /candlegraph

# Use entrypoint.sh
RUN chmod +x ./entrypoint.sh
ENTRYPOINT ["./entrypoint.sh"]
CMD ["python", "-m", "candlegraph.app"]
EXPOSE 8000

