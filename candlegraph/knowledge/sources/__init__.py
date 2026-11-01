"""Document sources: implementations of the ``Source`` Protocol.

A source returns :class:`~candlegraph.knowledge.documents.RawDocument`s and
never writes; ``documents.store_document`` owns the upsert and
``knowledge/news.py`` owns the budget, the cache and the ``source_call``
ledger around every call.
"""

from candlegraph.knowledge.sources.base import SearchResult, Source

__all__ = ["SearchResult", "Source"]
