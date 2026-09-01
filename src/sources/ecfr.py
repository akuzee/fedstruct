"""eCFR /api/admin/v1/agencies.json — the agency <-> CFR crosswalk (plan §1).

Verified 2026-09-01: 153 top-level, 316 total, depth 2. Every node carries
`cfr_references[{title, chapter}]`, which is the official mapping this project
hypothesised must exist and does. It is the join that gets you from an agency
to its regulations, and from there (via <AUTH>) to the statute that authorized
them.

This source does NOT publish organizational hierarchy. Its `children` are a
regulatory grouping, and treating them as reporting lines would be wrong. Only
Federal Register emits org edges.

Note it only includes agencies that HAVE regulations. That absence is itself a
signal for the availability registry, not a gap — hence the scope_claim below,
which is what licenses an ABSENT verdict for the regulatory_authority facet.
"""

from __future__ import annotations

import json

from .base import Parsed, Source, StatementRecord, UnitRecord

API = "https://www.ecfr.gov/api/admin/v1/agencies.json"


class ECFRAgencies(Source):
    name = "ecfr_agencies"
    url = API
    tier = "hash"
    publishes_hierarchy = False     # regulatory grouping, not reporting lines
    scope_claim = (
        "eCFR lists every agency with a chapter in the Code of Federal "
        "Regulations. Absence means the agency has no CFR chapter, which for "
        "a non-regulatory or non-executive body is correct rather than opaque."
    )

    def parse(self, body: bytes) -> Parsed:
        payload = json.loads(body.decode("utf-8-sig"))
        agencies = payload.get("agencies") if isinstance(payload, dict) else payload
        if not agencies:
            raise ValueError("eCFR agency list is empty — refusing to treat as data")

        units: list[UnitRecord] = []
        statements: list[StatementRecord] = []

        def walk(node: dict, parent_slug: str | None) -> None:
            slug = node.get("slug")
            if not slug:
                return
            detail = f"https://www.ecfr.gov/api/admin/v1/agencies.json#{slug}"

            units.append(UnitRecord(
                anchor_key=slug,
                name=node.get("name") or slug,
                short_name=node.get("short_name") or None,
                slug_hint=slug,
                # Recorded so the shape is not lost, but ingest will not turn
                # it into an org edge: publishes_hierarchy is False.
                parent_anchor_key=parent_slug,
                extra_keys={"ecfr_slug": slug},
                source_url=detail,
            ))

            statements.append(StatementRecord(
                unit_anchor_key=slug, predicate="exists", value="true",
                source_url=detail, source_locator=f"$.agencies[?slug={slug}]"))
            if node.get("name"):
                statements.append(StatementRecord(
                    unit_anchor_key=slug, predicate="name", value=node["name"],
                    source_url=detail))
            if node.get("short_name"):
                statements.append(StatementRecord(
                    unit_anchor_key=slug, predicate="short_name",
                    value=node["short_name"], source_url=detail))

            # One statement per chapter rather than a joined string, so that
            # gaining or losing a single chapter is a discrete structural
            # change rather than a whole-value rewrite.
            for ref in node.get("cfr_references") or []:
                title, chapter = ref.get("title"), ref.get("chapter")
                if title is None or chapter is None:
                    continue
                statements.append(StatementRecord(
                    unit_anchor_key=slug,
                    predicate="cfr_chapter",
                    value=f"{title} CFR chapter {chapter}",
                    source_url=f"https://www.ecfr.gov/current/title-{title}/chapter-{chapter}",
                    source_locator=f"$.agencies[?slug={slug}].cfr_references",
                    qualifiers={"title": title, "chapter": chapter},
                ))

            for child in node.get("children") or []:
                walk(child, slug)

        for a in agencies:
            walk(a, None)

        return Parsed(units=units, statements=statements)
