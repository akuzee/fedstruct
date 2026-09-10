"""The Source contract (plan §5).

A source knows how to turn bytes into records. It knows nothing about storage:
if a module under sources/ imports sqlite3 or builds a file path, the design
has failed. Everything a source emits is a frozen dataclass that ingest.py
applies to the ledger.

`allowed_predicates` is a hard allowlist and it is the mechanism that keeps a
known-bad source from poisoning good facts. GOVMAN's officials are stale to a
~2017 base, so GOVMAN cannot express a leadership fact -- not by convention,
but because the class refuses to emit one (plan §11 R14).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class UnitRecord:
    """A unit as one source sees it. Not yet reconciled with any other source."""
    anchor_key: str            # unique within this source
    name: str
    short_name: str | None = None
    slug_hint: str | None = None
    branch: str | None = None
    kind: str | None = None
    parent_anchor_key: str | None = None   # within the SAME source only
    extra_keys: dict[str, str] = field(default_factory=dict)  # scheme -> key
    source_url: str = ""


@dataclass(frozen=True)
class StatementRecord:
    unit_anchor_key: str
    predicate: str
    value: str | None = None
    value_num: float | None = None
    value_unit: str | None = None
    valid_precision: str = "unknown"
    effective_from: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    method: str = "published"
    confidence: float = 1.0
    source_url: str = ""
    source_locator: str | None = None
    qualifiers: dict | None = None


@dataclass(frozen=True)
class PositionRecord:
    anchor_key: str
    unit_anchor_key: str
    title: str
    appt_type: str | None = None
    pay_plan: str | None = None
    level_grade: str | None = None
    location: str | None = None
    is_pas: bool = False
    source_url: str = ""


@dataclass(frozen=True)
class OccupancyRecord:
    position_anchor_key: str
    first_name: str | None
    last_name: str | None
    status: str                      # current | ended
    is_acting: bool = False
    valid_from: str | None = None    # THIS person's tenure
    valid_to: str | None = None
    period_start: str | None = None  # the OFFICE's term
    period_end: str | None = None
    method: str = "published"
    confidence: float = 1.0
    source_url: str = ""


@dataclass(frozen=True)
class Parsed:
    units: list[UnitRecord] = field(default_factory=list)
    statements: list[StatementRecord] = field(default_factory=list)
    positions: list[PositionRecord] = field(default_factory=list)
    occupancies: list[OccupancyRecord] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        """The number used by the row-count sanity gate."""
        return len(self.units) or len(self.positions)


class Source:
    name: str = ""
    url: str = ""
    tier: str = "hash"              # declared | hash | uncond
    edge_kind: str = "org"
    raw_suffix: str = ".json"

    # The source's own completeness claim, quoted, with a URL.
    # None => this source may report PRESENT but may NEVER ground an ABSENT.
    scope_claim: str | None = None

    # None = every predicate. A set = a hard allowlist.
    allowed_predicates: frozenset[str] | None = None

    # Only a source that publishes its OWN hierarchy may emit edges. This is
    # never cross-source: FR's parent_id is FR's claim about FR's tree.
    publishes_hierarchy: bool = False

    def parse(self, body: bytes) -> Parsed:
        raise NotImplementedError

    def sniff(self, body: bytes) -> str | None:
        """Cheap shape check run at FETCH time, before anything is stored.

        Returns an error string if the body cannot be this source's data.
        Exists because a CDN block page can arrive as HTTP 200 (verified:
        escs.opm.gov via Akamai on GitHub runners) — and a block page that
        gets stored hashes stably, so the NEXT fetch of the same block page
        would read as 'unchanged' and the block would become invisible.
        """
        return None

    def check_allowed(self, parsed: Parsed) -> None:
        """Enforce the predicate allowlist. Called by ingest, and tested."""
        if self.allowed_predicates is None:
            return
        bad = {s.predicate for s in parsed.statements} - self.allowed_predicates
        if bad:
            raise ValueError(
                f"source {self.name!r} emitted disallowed predicates {sorted(bad)}; "
                f"allowlist is {sorted(self.allowed_predicates)}"
            )
        if parsed.occupancies and "holds_position" not in (self.allowed_predicates or ()):
            raise ValueError(
                f"source {self.name!r} emitted occupancies but is not allowed to "
                f"assert leadership facts"
            )
