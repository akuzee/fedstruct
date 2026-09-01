"""Normalization and the predicate table (plan §3).

Two independent axes, and conflating them is the mistake that makes a temporal
store grow like a snapshot store:

    versioned    does a change open a new validity interval at all?
    materiality  if it changes, is it news?

Intervals close on `value_norm`, never on `value_raw`. A capitalization fix, a
slug rename, or reworded prose bumps last_fetch_id and cosmetic_revisions in
place and produces zero rows and zero changelog lines. That single rule kills
the bulk of editorial churn BEFORE it becomes a diff, which is much better than
filtering it afterward -- and it is what keeps growth proportional to news
(~2-4k rows/yr instead of ~268k).
"""

from __future__ import annotations

import re
import unicodedata

# ── predicates ─────────────────────────────────────────────────────────────
# materiality: structural | nominal | quantitative | cosmetic
# versioned:   whether a change opens a new validity interval
#
# Finite and hand-written on purpose. There is no schema-inference layer: a new
# source maps onto these predicates or it does not get ingested (plan §11 R12).
PREDICATES: dict[str, tuple[str, bool]] = {
    "exists":            ("structural",   True),
    "name":              ("structural",   True),
    "cfr_chapter":       ("structural",   True),   # gain/loss of regulatory authority
    "toptier_code":      ("structural",   True),
    "short_name":        ("nominal",      True),
    "budget_authority":  ("quantitative", True),
    "obligations":       ("quantitative", True),
    "headcount_fte":     ("quantitative", True),
    "position_count":    ("quantitative", True),
    "position_count_pas": ("quantitative", True),
    "regulatory_bytes":  ("quantitative", True),
    "slug":              ("cosmetic",     False),
    "url":               ("cosmetic",     False),
    "agency_url":        ("cosmetic",     False),
    "logo_url":          ("cosmetic",     False),
    "description":       ("cosmetic",     False),  # raw_hash only; never a new row
    "mission_text":      ("cosmetic",     False),
    "wikidata_qid":      ("nominal",      True),
}

STRUCTURAL = frozenset(p for p, (m, _) in PREDICATES.items() if m == "structural")
VERSIONED = frozenset(p for p, (_, v) in PREDICATES.items() if v)

# Predicates a unit may hold MANY of at once. EPA does not have "a" CFR
# chapter, it has twelve, and each is an independent fact with its own validity
# interval — so gaining one is an opening, not an overwrite of another.
#
# For these the value itself discriminates rows (statements.value_key);
# everything else is single-valued and uses ''.
MULTIVALUED = frozenset({"cfr_chapter"})


def value_key(predicate: str, value_norm: str | None) -> str:
    return (value_norm or "") if predicate in MULTIVALUED else ""


def materiality(predicate: str) -> str:
    """Unknown predicates are 'cosmetic', i.e. they can never make the changelog.

    Failing closed matters: a predicate someone adds without classifying it
    should be silent, not headline news.
    """
    return PREDICATES.get(predicate, ("cosmetic", False))[0]


def is_versioned(predicate: str) -> bool:
    return PREDICATES.get(predicate, ("cosmetic", False))[1]


# ── name normalization ─────────────────────────────────────────────────────
# Every entry below is a pair actually observed differing between two sources
# or two fetches of the same source. This map is deliberately NOT clever: a
# normalizer that guesses is a normalizer that silently merges two real
# agencies, and a false merge invents edges that do not exist (plan §2).
_ABBREV = {
    "dept": "department",
    "dept.": "department",
    "&": "and",
    "u.s.": "united states",
    "us": "united states",
    "u.s": "united states",
    "natl": "national",
    "natl.": "national",
    "nat'l": "national",
    "comm'n": "commission",
    "commn": "commission",
    "adm": "administration",
    "adm.": "administration",
    "admin": "administration",
    "svc": "service",
    "svcs": "services",
    "off.": "office",
    "bur.": "bureau",
    "sec'y": "secretary",
    "asst": "assistant",
}

_PARENTHETICAL = re.compile(r"\([^)]*\)")
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def normalize_name(s: str | None) -> str:
    """NFKC, casefold, drop parentheticals, expand abbreviations, collapse space.

    Used for comparison and blocking only. The raw string is always retained
    alongside, because the UI quotes what the government wrote, not what we
    made of it.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).casefold()
    s = _PARENTHETICAL.sub(" ", s)
    s = _PUNCT.sub(" ", s)
    tokens = [_ABBREV.get(t, t) for t in _WS.split(s) if t]
    # An abbreviation may expand to multiple words ("u.s." -> "united states"),
    # so re-split after substitution.
    out: list[str] = []
    for t in tokens:
        out.extend(t.split())
    return " ".join(out)


def normalize_value(predicate: str, value: str | None) -> str | None:
    """The comparison form for a statement value.

    Name-ish predicates go through normalize_name so that "National Institutes
    Of Health" -> "National Institutes of Health" is a cosmetic revision rather
    than a rename. Everything else compares on stripped text.
    """
    if value is None:
        return None
    if predicate in ("name", "short_name"):
        return normalize_name(value)
    return _WS.sub(" ", value.strip())


_SLUG_BAD = re.compile(r"[^a-z0-9]+")


def slugify(s: str, maxlen: int = 80) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = _SLUG_BAD.sub("-", s.lower()).strip("-")
    return s[:maxlen].strip("-") or "unnamed"
