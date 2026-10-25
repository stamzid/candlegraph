"""Gap detection and the data-quality report.

A missing hour is not automatically a fault: Uniswap genuinely produces no
``tokenHourData`` for an hour with no swaps, and a thin token can be quiet for
a long time. So nothing here treats an absent hour as an error on its own. It
measures coverage, finds the runs of consecutive missing hours, and separately
flags rows that are impossible regardless of market conditions (``high < low``
and friends). The judgement about what is acceptable lives in one place:
``MIN_EXPECTED_COVERAGE``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from candlegraph.storage import DatabaseManager

HOUR = 3600


@dataclass(frozen=True)
class Gap:
    """A run of consecutive missing hours inside a token's stored range."""

    start_unix: int
    end_unix: int

    @property
    def hours(self) -> int:
        return (self.end_unix - self.start_unix) // HOUR + 1

    @property
    def start(self) -> datetime:
        return datetime.fromtimestamp(self.start_unix, tz=UTC)

    @property
    def end(self) -> datetime:
        return datetime.fromtimestamp(self.end_unix, tz=UTC)

    def __str__(self) -> str:
        return f"{self.start:%Y-%m-%d %H:%M}..{self.end:%Y-%m-%d %H:%M} ({self.hours}h)"


@dataclass
class TokenQuality:
    """The quality picture for one token."""

    token_id: str
    symbol: str
    stored_hours: int = 0
    expected_hours: int = 0
    first_unix: int | None = None
    last_unix: int | None = None
    gaps: list[Gap] = field(default_factory=list)

    @property
    def missing_hours(self) -> int:
        return max(0, self.expected_hours - self.stored_hours)

    @property
    def coverage(self) -> float:
        """Stored fraction of the hours inside the token's own stored range."""
        if self.expected_hours <= 0:
            return 0.0
        return self.stored_hours / self.expected_hours

    @property
    def longest_gap(self) -> Gap | None:
        return max(self.gaps, key=lambda gap: gap.hours, default=None)

    @property
    def first(self) -> datetime | None:
        return datetime.fromtimestamp(self.first_unix, tz=UTC) if self.first_unix else None

    @property
    def last(self) -> datetime | None:
        return datetime.fromtimestamp(self.last_unix, tz=UTC) if self.last_unix else None


@dataclass
class QualityReport:
    """The whole-database quality picture for one point in time."""

    generated_at: datetime
    tokens: list[TokenQuality] = field(default_factory=list)
    missing_tokens: list[str] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)
    completeness: dict[str, Any] = field(default_factory=dict)
    min_expected_coverage: float = 0.8

    @property
    def total_rows(self) -> int:
        return sum(token.stored_hours for token in self.tokens)

    @property
    def under_covered(self) -> list[TokenQuality]:
        """Tokens whose coverage is below the configured threshold."""
        return [t for t in self.tokens if t.coverage < self.min_expected_coverage]

    @property
    def ok(self) -> bool:
        """True when nothing needs a human to look at it."""
        return not self.violations and not self.under_covered and not self.missing_tokens


def group_missing_hours(missing: list[int]) -> list[Gap]:
    """Collapse a sorted list of missing hour starts into contiguous runs.

    Reporting "3 gaps, longest 14h" is actionable; reporting 212 individual
    missing hours is not.
    """
    gaps: list[Gap] = []
    for unix in sorted(missing):
        if gaps and unix == gaps[-1].end_unix + HOUR:
            gaps[-1] = Gap(gaps[-1].start_unix, unix)
        else:
            gaps.append(Gap(unix, unix))
    return gaps


def find_gaps(
    db: DatabaseManager, token_id: str, start_unix: int | None = None, end_unix: int | None = None
) -> list[Gap]:
    """Contiguous runs of missing hours for a token, inside its stored range."""
    rows = db.fetch_all(
        db.sql.missing_hours,
        {"token_id": token_id, "start_unix": start_unix, "end_unix": end_unix},
    )
    return group_missing_hours([int(row["period_start_unix"]) for row in rows])


def build_report(
    db: DatabaseManager,
    expected_symbols: tuple[str, ...] = (),
    start_unix: int | None = None,
    min_expected_coverage: float = 0.8,
    violation_limit: int = 50,
    now: datetime | None = None,
) -> QualityReport:
    """Assemble the quality report.

    Args:
        db: Where to read from.
        expected_symbols: Symbols that ought to be present; any that have no
            rows at all are listed in ``missing_tokens``.
        start_unix: Only consider hours at or after this time.
        min_expected_coverage: Coverage below which a token is flagged.
        violation_limit: Cap on impossible rows listed.
        now: Report timestamp, for deterministic tests.

    Returns:
        A :class:`QualityReport`; ``ok`` is True when nothing needs attention.
    """
    report = QualityReport(
        generated_at=now or datetime.now(tz=UTC),
        min_expected_coverage=min_expected_coverage,
    )

    for row in db.fetch_all(db.sql.coverage_report, {"start_unix": start_unix}):
        token = TokenQuality(
            token_id=row["token_id"],
            symbol=row["symbol"] or row["token_id"],
            stored_hours=int(row["stored_hours"] or 0),
            expected_hours=int(row["expected_hours"] or 0),
            first_unix=int(row["first_unix"]) if row["first_unix"] is not None else None,
            last_unix=int(row["last_unix"]) if row["last_unix"] is not None else None,
        )
        if token.missing_hours:
            token.gaps = find_gaps(db, token.token_id, start_unix)
        report.tokens.append(token)

    seen = {token.symbol.casefold() for token in report.tokens}
    report.missing_tokens = [s for s in expected_symbols if s.casefold() not in seen]

    report.violations = db.fetch_all(db.sql.ohlc_violations, {"limit": violation_limit})
    rows = db.fetch_all(db.sql.field_completeness, {"start_unix": start_unix})
    report.completeness = dict(rows[0]) if rows else {}

    return report


def format_report(report: QualityReport) -> str:
    """Render a report as a fixed-width table for the CLI and CI logs."""
    lines = [
        f"Data-quality report  {report.generated_at:%Y-%m-%d %H:%M:%S} UTC",
        f"{'symbol':<10}{'rows':>8}{'coverage':>10}{'gaps':>7}{'longest':>10}  range",
        "-" * 86,
    ]
    for token in sorted(report.tokens, key=lambda t: t.symbol):
        longest = token.longest_gap
        span = (
            f"{token.first:%Y-%m-%d %H:%M} .. {token.last:%Y-%m-%d %H:%M}"
            if token.first and token.last
            else "-"
        )
        lines.append(
            f"{token.symbol:<10}{token.stored_hours:>8}{token.coverage:>9.1%}"
            f"{len(token.gaps):>7}{(f'{longest.hours}h' if longest else '-'):>10}  {span}"
        )

    lines.append("-" * 86)
    lines.append(f"{'total':<10}{report.total_rows:>8}")

    if report.completeness:
        total = int(report.completeness.get("rows_total") or 0)
        if total:
            parts = [
                f"{name.removesuffix('_present')}={int(report.completeness.get(name) or 0) / total:.0%}"
                for name in ("volume_present", "volume_usd_present", "tvl_usd_present", "fees_usd_present")
            ]
            lines.append("field completeness: " + "  ".join(parts))

    if report.missing_tokens:
        lines.append(f"MISSING (no rows at all): {', '.join(report.missing_tokens)}")
    if report.under_covered:
        flagged = ", ".join(f"{t.symbol} {t.coverage:.1%}" for t in report.under_covered)
        lines.append(f"BELOW {report.min_expected_coverage:.0%} COVERAGE: {flagged}")
    if report.violations:
        lines.append(f"IMPOSSIBLE ROWS: {len(report.violations)} (high<low or OHLC out of range)")
    if report.ok:
        lines.append("OK: no gaps beyond threshold, no impossible rows, every expected token present.")

    return "\n".join(lines)
