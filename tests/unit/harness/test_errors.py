"""Which failures are worth trying again.

The direction of the default is the point of most of these: an unknown
exception is **permanent**, so a new failure mode fails once, visibly,
instead of being retried three times and reported as whatever the last
attempt happened to say.
"""

from __future__ import annotations

import pytest

from candlegraph.harness.errors import ErrorClass, classify


class SubgraphError(RuntimeError):
    """The real one, reproduced by name: classification reads the name, not the import."""


class TavilyRateLimitError(RuntimeError):
    pass


class NewsError(RuntimeError):
    pass


class EmbeddingError(RuntimeError):
    pass


class PriceError(LookupError):
    pass


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("connection refused"),
        ConnectionResetError("reset by peer"),
        TimeoutError("timed out"),
        EmbeddingError("ollama is still pulling the model"),
    ],
)
def test_a_blink_is_transient(exc: Exception) -> None:
    assert classify(exc) is ErrorClass.TRANSIENT
    assert classify(exc).retryable


def test_a_rate_limit_is_its_own_class() -> None:
    """Retryable, but it wants a longer wait than a dropped connection."""
    assert classify(TavilyRateLimitError("432")) is ErrorClass.RATE_LIMITED
    assert classify(TavilyRateLimitError("432")).retryable


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("window must be at least 2"),
        TypeError("expected a float"),
        KeyError("symbol"),
        NewsError("no TAVILY_API_KEY"),
        PriceError("no stored price history for WBTC"),
    ],
)
def test_a_mistake_is_permanent(exc: Exception) -> None:
    """Retrying a bad parameter spends the deadline to fail the same way."""
    assert classify(exc) is ErrorClass.PERMANENT
    assert not classify(exc).retryable


def test_a_missing_key_is_not_a_blip_even_though_it_looks_like_a_network_error() -> None:
    """`NewsError` wraps "no API key" as often as "the call failed"."""
    assert classify(NewsError("tavily: no credentials")) is ErrorClass.PERMANENT


def test_an_unknown_exception_is_permanent() -> None:
    """The conservative direction: fail once and visibly, not three times."""

    class SomethingNewError(Exception):
        pass

    assert classify(SomethingNewError("?")) is ErrorClass.PERMANENT


def test_a_subclass_is_classified_as_its_parent() -> None:
    class SpecificTimeoutError(TimeoutError):
        pass

    assert classify(SpecificTimeoutError("slow")) is ErrorClass.TRANSIENT


def test_a_subgraph_error_is_read_for_its_status_code() -> None:
    """The one place a message is parsed, because the type carries both cases."""
    assert classify(SubgraphError("gateway returned 503")) is ErrorClass.TRANSIENT
    assert classify(SubgraphError("gateway returned 429")) is ErrorClass.TRANSIENT
    assert classify(SubgraphError("bad query: field 'nope' not found")) is ErrorClass.PERMANENT


def test_classifying_imports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant 1's shape: the classifier runs wherever the scheduler does."""
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import sys, candlegraph.harness.errors as e;"
        "e.classify(ValueError('x'));"
        "bad = [m for m in ('httpx', 'psycopg2', 'pandas', 'langgraph') if m in sys.modules];"
        "print(','.join(bad))"
    )
    root = Path(__file__).resolve().parents[3]
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=root)
    assert out.stdout.strip() == ""
