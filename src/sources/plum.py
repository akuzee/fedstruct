"""OPM PLUM — the people layer (plan §1).

The paper Plum Book is dead. The PLUM Act (5 U.S.C. § 3330f) replaced it with a
continuously-updated public directory effective 1 Jan 2026, and the backing API
is undocumented but public, unauthenticated, and CORS-open.

Verified 2026-09-01 against the live CSV: 15,777 rows; 174 AgencyName values;
1,442 distinct (Agency, Organization) pairs; 12,826 Filled; 6,846 filled with
no vacate date (current incumbents); 1,447 PAS.

Three things make this the best officeholder source available:

  * `OrganizationName` gives sub-agency granularity that Federal Register and
    eCFR (2 levels) do not.
  * Vacated incumbencies are RETAINED, so history is genuinely backfillable
    rather than only accruing forward. Ingest them on run #1 (plan §6).
  * `ExpirationDate` / `Tenure` describe the OFFICE's term, which maps onto
    period_start/period_end, distinct from the individual's tenure. That
    separation is what makes acting officials and vacancies representable.

Known incompleteness, which the registry must surface rather than hide:
GAO-26-108164 (Feb 2026) found >=7 federal entities and >=130 PAS positions
missing entirely, plus inconsistent identifiers and duplicate rows. OPM
concurred. So an absence here renders as "not listed in PLUM", never as
"no positions".

The join key is the hard part: rows carry no identifiers at all, only UPPERCASE
free text.
"""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime

from .base import OccupancyRecord, Parsed, PositionRecord, Source, StatementRecord, UnitRecord

API = "https://escs.opm.gov/escs-net/api/pbpub/download-data"
PORTAL = "https://www.opm.gov/about-us/open-government/plum-reporting/plum-data/"

# OPM renamed every column between Sept 2026 and Sept 2026 without notice
# (`AgencyName` -> `Agency`, `PositionTitle` -> `Position Title`, ...). Read
# through an alias map rather than a fixed header: the content-addressed
# archive in state/raw/ holds files in BOTH spellings, and `parse --reparse`
# has to keep working over all of them.
#
# The rename also brought two genuine improvements: `Position Status` now
# labels historical rows explicitly instead of calling them "Filled", and
# `Individual Unique ID` is the first person identifier PLUM has ever
# published.
FIELDS = {
    "agency":      ("Agency", "AgencyName"),
    "org":         ("Organization", "OrganizationName"),
    "title":       ("Position Title", "PositionTitle"),
    "status":      ("Position Status", "PositionStatus"),
    "appt":        ("Appointment Type", "AppointmentTypeDescription"),
    "expires":     ("Expiration Date", "ExpirationDate"),
    "grade":       ("Level, Grade, or Pay", "LevelGradePay"),
    "location":    ("Duty Location", "Location"),
    "first":       ("First Name", "IncumbentFirstName"),
    "last":        ("Last Name", "IncumbentLastName"),
    "person_id":   ("Individual Unique ID",),          # new in the 2026 rename
    "pay_plan":    ("Pay Plan", "PaymentPlanDescription"),
    "tenure":      ("Tenure",),
    "begin":       ("Begin Date", "IncumbentBeginDate"),
    "vacate":      ("Vacate Date", "IncumbentVacateDate"),
}

# Old files carried a zero time component; new ones are bare dates.
_DATE_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y", "%Y-%m-%d")


def _get(row: dict, field: str) -> str:
    for name in FIELDS[field]:
        if name in row:
            return (row.get(name) or "").strip()
    return ""


def parse_date(s: str | None) -> str | None:
    """-> ISO date, or None. Never guess: an unparseable date is absent."""
    if not s or not s.strip():
        return None
    s = s.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _key(*parts: str) -> str:
    """A stable surrogate for rows that carry no identifier.

    The natural key is the full uppercase tuple, hashed for length. It is
    stable exactly as long as OPM's strings are — and when they drift, the
    result is a false SPLIT (recoverable, visible in `unresolved`) rather than
    a false merge (which would invent an officeholder).
    """
    raw = "|".join(p.strip().upper() for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class PlumPositions(Source):
    name = "plum"
    url = API
    tier = "hash"
    raw_suffix = ".csv"
    publishes_hierarchy = True      # its own two-level tree, in its own namespace
    scope_claim = (
        "OPM PLUM is the statutory public directory of federal policy and "
        "supporting positions under 5 U.S.C. § 3330f. Agencies certify their "
        "data at least annually. GAO-26-108164 (Feb 2026) documented material "
        "gaps: at least 7 entities and 130 PAS positions missing."
    )

    def sniff(self, body: bytes) -> str | None:
        # A CDN can serve a block page as HTTP 200, so verify the body is
        # actually a PLUM CSV. Accept EITHER header spelling — see FIELDS.
        head = body[:512].decode("utf-8-sig", errors="replace")
        if not (head.startswith("Agency,") or head.startswith("AgencyName,")):
            return f"expected the PLUM CSV header, got {head[:80]!r}"
        for field in ("title", "status", "first", "last"):
            if not any(name in head for name in FIELDS[field]):
                return f"PLUM CSV is missing a {field!r} column: {head[:120]!r}"
        return None

    def parse(self, body: bytes) -> Parsed:
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        if not rows:
            raise ValueError("PLUM CSV is empty — refusing to treat as data")

        units: dict[str, UnitRecord] = {}
        positions: dict[str, PositionRecord] = {}
        occupancies: list[OccupancyRecord] = []
        statements: list[StatementRecord] = []
        counts: dict[str, dict[str, int]] = {}

        for i, r in enumerate(rows, start=2):   # 2 = first data row, 1 = header
            agency = _get(r, "agency")
            org = _get(r, "org")
            title = _get(r, "title")
            if not agency or not title:
                continue

            agency_key = _key(agency)
            if agency_key not in units:
                units[agency_key] = UnitRecord(
                    anchor_key=agency_key, name=_titlecase(agency),
                    extra_keys={"plum_agency": agency.upper()}, source_url=PORTAL)

            # PLUM often repeats the agency name as the organization. That is
            # not a self-parenting sub-unit, it is "the department itself".
            if org and org.upper() != agency.upper():
                unit_key = _key(agency, org)
                if unit_key not in units:
                    units[unit_key] = UnitRecord(
                        anchor_key=unit_key, name=_titlecase(org),
                        parent_anchor_key=agency_key,
                        extra_keys={"plum_agency_org": f"{agency.upper()}|{org.upper()}"},
                        source_url=PORTAL)
            else:
                unit_key = agency_key

            appt = _get(r, "appt") or None
            is_pas = appt == "PAS"
            pos_key = _key(agency, org, title)

            if pos_key not in positions:
                positions[pos_key] = PositionRecord(
                    anchor_key=pos_key, unit_anchor_key=unit_key, title=title,
                    appt_type=appt, pay_plan=_get(r, "pay_plan") or None,
                    level_grade=_get(r, "grade") or None,
                    location=_get(r, "location") or None,
                    is_pas=is_pas, source_url=PORTAL)
                c = counts.setdefault(unit_key, {"total": 0, "pas": 0})
                c["total"] += 1
                c["pas"] += int(is_pas)

            first = _get(r, "first") or None
            last = _get(r, "last") or None
            if not (first or last):
                continue    # a genuinely vacant position: no occupancy to assert

            begin = parse_date(_get(r, "begin"))
            vacate = parse_date(_get(r, "vacate"))
            # 'Filled' now means CURRENTLY filled: the rename split historical
            # rows out into their own status instead of labelling them Filled.
            filled = _get(r, "status") == "Filled"

            occupancies.append(OccupancyRecord(
                position_anchor_key=pos_key, first_name=first, last_name=last,
                # OPM's own person identifier, when published. Without it,
                # people are matched by name — which silently conflates the 80
                # distinct officials who share a name with another official.
                person_key=_get(r, "person_id") or None,
                # A vacate date closes the tenure regardless of the row's
                # status flag; historical rows are how this source backfills.
                status="current" if (filled and not vacate) else "ended",
                valid_from=begin, valid_to=vacate,
                # The OFFICE's term, identical for every holder under this
                # authority — not this person's service.
                period_end=parse_date(_get(r, "expires")),
                source_url=PORTAL,
            ))

        for unit_key, c in counts.items():
            statements.append(StatementRecord(
                unit_anchor_key=unit_key, predicate="position_count",
                value_num=float(c["total"]), value_unit="count", source_url=PORTAL))
            statements.append(StatementRecord(
                unit_anchor_key=unit_key, predicate="position_count_pas",
                value_num=float(c["pas"]), value_unit="count", source_url=PORTAL))

        return Parsed(units=list(units.values()), statements=statements,
                      positions=list(positions.values()), occupancies=occupancies)


_LOWER = {"of", "the", "and", "for", "in", "on", "to", "a", "an"}
_UPPER = {"us", "usa", "epa", "fbi", "cia", "nasa", "hhs", "dhs", "doj", "dod",
          "hud", "va", "gsa", "sba", "nsf", "fcc", "sec", "ftc", "nrc", "irs",
          "atf", "dea", "tsa", "fema", "cdc", "nih", "fda", "usda", "doe", "dot"}


def _titlecase(s: str) -> str:
    """PLUM ships everything uppercase; render it readably without losing acronyms.

    The raw string is always retained alongside — this only affects display.
    """
    words = s.lower().split()
    out = []
    for i, w in enumerate(words):
        stripped = w.strip(",.()")
        if stripped in _UPPER:
            out.append(w.upper())
        elif i and stripped in _LOWER:
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)
