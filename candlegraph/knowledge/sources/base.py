"""The ``Source`` Protocol: what any document source looks like from outside.

Implementations fetch and parse; they never touch the database and never
decide whether they may spend. The pipeline in :mod:`candlegraph.knowledge.news`
wraps every call in the budget guard, the ``(asset, UTC day)`` cache and a
``source_call`` row — including refusals, which a source never even sees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from candlegraph.knowledge.documents import RawDocument


@dataclass(frozen=True)
class SearchResult:
    """One search's harvest, plus what it cost."""

    documents: list[RawDocument]
    credits: int
    """What the call cost, as the source understands its own pricing."""
    results: int
    """Raw result count before empty-content results were dropped."""
    skipped: int = 0
    """Results dropped for having no usable text."""
    params: dict[str, object] = field(default_factory=dict)
    """The request as sent, for the ledger's ``params`` column."""


class Source(Protocol):
    """A place documents come from. Returns raw documents; never writes."""

    @property
    def id(self) -> str:
        """The ``source`` table row this writes under (``tavily``, ``rss:<feed>``)."""
        ...

    @property
    def kind(self) -> str:
        """``search`` | ``feed`` | ``file``."""
        ...

    @property
    def cost(self) -> int:
        """Credits one ``search`` call will spend, for the budget guard."""
        ...

    def search(self, query: str, *, start: datetime, end: datetime, max_results: int) -> SearchResult:
        """Fetch documents for ``query`` published inside ``[start, end]``."""
        ...
