"""The design documents and post-mortems, addressed by URI.

``docs/knowledge/mcp/DESIGN.md``: *"Resources (read-only, URI-addressed) suit
the design documents and the incident post-mortems. A client can pull the
relevant document into context instead of the operator pasting it."*

The mapping is the directory layout, unchanged:

======================================  ==========================================
``docs/roadmap/DESIGN.md``              ``candlegraph://docs/roadmap``
``docs/knowledge/graph/DESIGN.md``      ``candlegraph://docs/knowledge/graph``
``docs/rollout/ROLLOUT.md``             ``candlegraph://docs/rollout``
``docs/incidents/<slug>/POSTMORTEM.md`` ``candlegraph://incidents/<slug>``
======================================  ==========================================

So a URI is guessable from a path and vice versa, and a document added to
``docs/`` is served without anyone editing this file. Everything here is a
pure function of the tree; the server turns each entry into a resource whose
reader is bound to that one file, so there is no path to traverse.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

SCHEME = "candlegraph"
# The file names `docs/README.md` describes. Anything else in the tree
# (an image, a scratch note) is not a document and is not served.
DESIGN = "DESIGN.md"
POSTMORTEM = "POSTMORTEM.md"
ROLLOUT = "ROLLOUT.md"

_HEADING = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_STATUS = re.compile(r"^\*\*Status:\*\*\s*(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class DocumentResource:
    """One Markdown document the server offers, with its address."""

    uri: str
    path: Path
    title: str
    status: str
    """The document's own Status line, or empty. Says whether it describes
    code or an idea, which is the first thing a reader needs to know."""

    @property
    def description(self) -> str:
        kind = "post-mortem" if self.path.name == POSTMORTEM else "design document"
        suffix = f" — status: {self.status}" if self.status else ""
        return f"{self.title} ({kind}){suffix}"


def uri_for(relative: Path) -> str | None:
    """The URI for a path relative to ``docs/``, or ``None`` if it is not a document.

    Pure string handling, so the rule can be read and tested without a tree.
    """
    parts = relative.parts
    if len(parts) < 2 or parts[-1] not in (DESIGN, POSTMORTEM, ROLLOUT):
        return None
    directory = "/".join(parts[:-1])
    if parts[0] == "incidents" and parts[-1] == POSTMORTEM:
        return f"{SCHEME}://incidents/{'/'.join(parts[1:-1])}"
    return f"{SCHEME}://docs/{directory}"


def title_and_status(text: str) -> tuple[str, str]:
    """The document's first heading and its Status line. Either may be empty."""
    heading = _HEADING.search(text)
    status = _STATUS.search(text)
    return (heading.group(1) if heading else "", status.group(1) if status else "")


def document_resources(docs_root: Path) -> list[DocumentResource]:
    """Every design document and post-mortem under ``docs_root``, sorted by URI.

    A missing or unreadable tree yields nothing rather than raising: the
    server is useful without its documents, and a packaging mistake should
    not stop an operator reading the ledger.
    """
    return sorted(_walk(docs_root), key=lambda resource: resource.uri)


def _walk(docs_root: Path) -> Iterator[DocumentResource]:
    if not docs_root.is_dir():
        return
    for path in sorted(docs_root.rglob("*.md")):
        uri = uri_for(path.relative_to(docs_root))
        if uri is None:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - unreadable file on a dev machine
            continue
        title, status = title_and_status(text)
        yield DocumentResource(uri=uri, path=path, title=title or path.parent.name, status=status)
