"""The citation validator: prose may only cite what retrieval returned.

Pure, and the reason "cannot invent a citation" is a property of the system
rather than a hope about the model (``docs/knowledge/graph/DESIGN.md``, rule
one). A rationale arrives with ``[n]`` markers; the retrieved set is the
allowed ids. Any sentence carrying an id outside that set is **removed**,
not repaired — rewriting the model's sentence would be the validator
inventing prose of its own — and the result says which ids were rejected
and which sentences went, so the explanation can carry the flag.

Invariant 22's shape: the numbers and the citations are computed; the prose
is optional and is the only thing this module touches.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from candlegraph.knowledge.documents import split_paragraphs, split_sentences

# `[1]`, `[1][2]`, and the `[1, 2]` form a model sometimes writes.
_MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
# Markers written after the full stop ("…fell. [1] Next…") belong to the
# sentence they follow, not the one they precede.
_LEADING_MARKERS = re.compile(r"^(?:\[\d+(?:\s*,\s*\d+)*\]\s*)+")


@dataclass(frozen=True)
class Validation:
    """What survived, and what did not."""

    rationale: str
    """The accepted text: every surviving sentence, one paragraph."""
    cited: tuple[int, ...]
    """Allowed ids the accepted text cites, in order of first appearance."""
    rejected: tuple[int, ...]
    """Ids found outside the allowed set, in order of first appearance."""
    stripped: tuple[str, ...]
    """The sentences removed for carrying a rejected id."""

    @property
    def clean(self) -> bool:
        """True when nothing had to be removed."""
        return not self.rejected


def citation_ids(text: str) -> list[int]:
    """Every ``[n]`` id in ``text``, in order, duplicates kept."""
    ids: list[int] = []
    for match in _MARKER.finditer(text):
        ids.extend(int(part) for part in match.group(1).split(","))
    return ids


def sentences_of(text: str) -> list[str]:
    """Sentences across paragraphs, with trailing markers kept on their sentence."""
    out: list[str] = []
    for paragraph in split_paragraphs(text):
        for sentence in split_sentences(paragraph):
            lead = _LEADING_MARKERS.match(sentence)
            if lead and out:
                out[-1] = f"{out[-1]} {lead.group(0).strip()}"
                sentence = sentence[lead.end() :]
                if not sentence:
                    continue
            out.append(sentence)
    return out


def validate_rationale(text: str, allowed: Iterable[int]) -> Validation:
    """Keep the sentences whose citations are all in ``allowed``.

    Args:
        text: The narrator's paragraph(s), with ``[n]`` markers.
        allowed: The citation numbers retrieval produced (``1..k``).

    Returns:
        A :class:`Validation`. An empty ``text`` is clean and empty; a text
        whose every sentence is rejected yields an empty rationale with the
        rejections listed, which the caller renders as a flag rather than
        as silence.
    """
    permitted = set(allowed)
    kept: list[str] = []
    cited: dict[int, None] = {}
    rejected: dict[int, None] = {}
    stripped: list[str] = []

    for sentence in sentences_of(text):
        ids = citation_ids(sentence)
        bad = [n for n in ids if n not in permitted]
        if bad:
            for n in bad:
                rejected.setdefault(n, None)
            stripped.append(sentence)
            continue
        for n in ids:
            cited.setdefault(n, None)
        kept.append(sentence)

    return Validation(
        rationale=" ".join(kept),
        cited=tuple(cited),
        rejected=tuple(rejected),
        stripped=tuple(stripped),
    )
