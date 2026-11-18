"""Is this failure worth trying again?

One function, :func:`classify`, so the answer is given in one place rather
than guessed at each call site. The pipeline executor is its only caller
today (``docs/harness/pipelines/DESIGN.md`` §6.5).

Three classes, and the distinction that matters is not "how bad" but "would
doing it again help":

- **transient** — the network blinked, Postgres restarted, a service was
  briefly down. Retrying is the whole remedy.
- **rate limited** — the far end said "not so fast". Retrying helps, but only
  after longer than a transient error deserves.
- **permanent** — a bad config, a missing key, a validation error, a bug.
  Retrying cannot help and spends the deadline pretending otherwise.

Classification is by exception type and, where the type is too broad, by what
the exception says. Matching on message text is a last resort and each use
says why.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorClass(StrEnum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    PERMANENT = "permanent"

    @property
    def retryable(self) -> bool:
        return self is not ErrorClass.PERMANENT


# Exception type names that are always transient, by name rather than by
# import: classifying must not drag httpx, psycopg2 and the knowledge layer
# into a process that only schedules jobs (invariant 1).
_TRANSIENT_NAMES = frozenset(
    {
        # httpx
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "RemoteProtocolError",
        "TimeoutException",
        # psycopg2 / the pool
        "OperationalError",
        "InterfaceError",
        "AdminShutdown",
        "DatabaseError",
        # the embedder, when Ollama is down or still pulling
        "EmbeddingError",
        # stdlib
        "ConnectionError",
        "ConnectionResetError",
        "TimeoutError",
        "BrokenPipeError",
    }
)

_RATE_LIMITED_NAMES = frozenset({"TavilyRateLimitError", "RateLimitError"})

# Names that look transient but are not. `NewsError` wraps a missing API key
# as often as a failed call, and `PriceError` means "nothing is stored",
# which another attempt will not change.
_PERMANENT_NAMES = frozenset(
    {
        "NewsError",
        "PriceError",
        "ValidationError",
        "ValueError",
        "TypeError",
        "KeyError",
        "AttributeError",
        "ImportError",
        "PipelineError",
    }
)


def classify(exc: BaseException) -> ErrorClass:
    """What kind of failure ``exc`` is, for the retry decision.

    Unknown exception types are **permanent**. That is the conservative
    direction: a wrongly-permanent error fails one run visibly, while a
    wrongly-transient one burns the deadline three times over and then fails
    anyway, having hidden the real message behind two retries.
    """
    for kind in type(exc).__mro__:
        name = kind.__name__
        if name in _RATE_LIMITED_NAMES:
            return ErrorClass.RATE_LIMITED
        if name in _PERMANENT_NAMES:
            return ErrorClass.PERMANENT
        if name in _TRANSIENT_NAMES:
            return ErrorClass.TRANSIENT

    # SubgraphError covers both "the gateway returned 503" and "the query is
    # malformed". The status code is in the message, and 5xx is the half
    # worth retrying — the only place here that reads text, because the type
    # genuinely does not carry the distinction.
    if type(exc).__name__ == "SubgraphError":
        text = str(exc)
        return ErrorClass.TRANSIENT if any(code in text for code in _SERVER_CODES) else ErrorClass.PERMANENT
    return ErrorClass.PERMANENT


_SERVER_CODES = ("500", "502", "503", "504", "429")
