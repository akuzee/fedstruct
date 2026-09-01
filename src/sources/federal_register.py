"""Federal Register /agencies — the structure spine (plan §1).

Verified 2026-09-01: 472 agencies, 247 with parent_id NULL, depth <= 3. Both
directions of every edge are published (parent_id AND child_ids), so the tree
needs no inference at all.

This is the ONLY source permitted to emit organizational hierarchy. eCFR's tree
is regulatory, USAspending's is financial, and PLUM's is a two-level reporting
convenience; flattening any of those into this one would silently corrupt the
org chart.

Two honest caveats to carry into the UI:
  * The list is scoped to agencies that PUBLISH in the Federal Register, and it
    keeps defunct bodies as first-class entries with no tombstone field. There
    is no `active` flag, so "disappeared" is genuinely ambiguous (plan §6).
  * There is no `updated_at` and no changelog, which is why this source is
    hash-gated rather than declared-freshness.
"""

from __future__ import annotations

import json

from .base import Parsed, Source, StatementRecord, UnitRecord

API = "https://www.federalregister.gov/api/v1/agencies.json"


class FederalRegisterAgencies(Source):
    name = "fr_agencies"
    url = API
    tier = "hash"
    publishes_hierarchy = True
    scope_claim = (
        "The Office of the Federal Register publishes this list as the set of "
        "agencies that submit documents for publication in the Federal "
        "Register. It includes defunct agencies and carries no active flag."
    )

    def parse(self, body: bytes) -> Parsed:
        data = json.loads(body.decode("utf-8-sig"))
        if not isinstance(data, list):
            raise ValueError("expected a JSON array of agencies")
        if not data:
            # Belt and braces alongside http.SourceBlocked: an empty agency
            # list must never be treated as "the government abolished
            # everything" (plan §11 R5).
            raise ValueError("agency list is empty — refusing to treat as data")

        by_id = {a["id"]: a for a in data if a.get("id") is not None}
        units: list[UnitRecord] = []
        statements: list[StatementRecord] = []

        for a in data:
            aid = a.get("id")
            if aid is None:
                continue
            key = str(aid)
            detail_url = a.get("json_url") or f"https://www.federalregister.gov/api/v1/agencies/{aid}.json"

            parent = a.get("parent_id")
            # Guard against a dangling parent rather than minting a phantom
            # unit for it.
            parent_key = str(parent) if parent is not None and parent in by_id else None

            extra = {"fr_slug": a["slug"]} if a.get("slug") else {}

            units.append(UnitRecord(
                anchor_key=key,
                name=a.get("name") or f"agency {aid}",
                short_name=a.get("short_name") or None,
                slug_hint=a.get("slug"),
                parent_anchor_key=parent_key,
                extra_keys=extra,
                source_url=detail_url,
            ))

            def stmt(pred: str, val, **kw) -> None:
                if val in (None, ""):
                    return
                statements.append(StatementRecord(
                    unit_anchor_key=key, predicate=pred, value=str(val),
                    source_url=detail_url, source_locator=f"$.{pred}", **kw))

            stmt("exists", "true")
            stmt("name", a.get("name"))
            stmt("short_name", a.get("short_name"))
            stmt("slug", a.get("slug"))
            stmt("agency_url", a.get("agency_url"))
            stmt("url", a.get("url"))
            # Prose carrying statutory history: "ACTION was established by
            # Reorganization Plan No. 1 of 1971...". Cosmetic for versioning
            # (it is reworded without meaning changing), but it is the only
            # signal for defunctness anywhere in this source.
            stmt("description", a.get("description"))
            logo = (a.get("logo") or {}).get("medium_url")
            stmt("logo_url", logo)

        return Parsed(units=units, statements=statements)
