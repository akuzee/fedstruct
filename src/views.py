"""SQLite -> static JSON (plan §7).

No server. `views.build()` writes deterministic files that a zero-build
vanilla-JS page reads directly, which is how every web project in this repo
works and what makes GitHub Pages hosting free.

Two invariants, both testable:

  1. Every fact carries its citation. A unit page is literally a projection of
     statement rows, each with source_url, retrieved_at, method and confidence.
  2. The build is deterministic — sorted keys, no wall-clock values, stable
     float formatting. Running it twice produces byte-identical files, which
     is what "output files are pure derived views" actually means.
"""

from __future__ import annotations

import json
from pathlib import Path

# Weakest link, not average: a claim is exactly as good as its shakiest step.
# Averaging is how a fuzzy name match gets laundered into a fact.
METHOD_CONFIDENCE = {
    "published": 1.00,
    "crosswalk": 0.95,
    "derived": 0.90,
    "name_exact": 0.85,   # exact normalized name made unambiguous by uniqueness
                          # (plan §7 join order). Renders as "linked", never
                          # "published" — a name join is visibly not an id join.
    "adjudicated": 0.80,
    "inferred": 0.60,
    "asserted": 0.50,
}
# Three bands, never a raw number: "0.83" implies a precision the pipeline
# does not have.
BANDS = ((0.90, "published"), (0.75, "linked"), (0.0, "inferred"))


# Which source is authoritative for a single-valued fact. Federal Register is
# the source of record for agency identity and the only publisher of
# organizational hierarchy, so it outranks the rest.
SOURCE_PRIORITY = ("fr_agencies", "ecfr_agencies", "usaspending", "plum",
                   "congress", "govman", "wikidata")


def _src_rank(source: str) -> int:
    try:
        return SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(SOURCE_PRIORITY)


def band(conf: float) -> str:
    for floor, name in BANDS:
        if conf >= floor:
            return name
    return "inferred"


def build(conn, out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    (out_dir / "units").mkdir(parents=True, exist_ok=True)

    canon = {r["id"]: r["canonical_id"] for r in conn.execute("SELECT * FROM v_canonical")}
    units = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, slug, canonical_name, name_norm, anchor_source, anchor_key, "
        "lifecycle, merged_into, merge_method FROM units")}
    caveats = _caveats(conn)

    # Which sources contributed to each canonical unit — the basis of both the
    # reconciliation metric and the coverage strip.
    sources_for: dict[int, set[str]] = {}
    for uid, u in units.items():
        sources_for.setdefault(canon.get(uid, uid), set()).add(u["anchor_source"])

    registry, written = [], 0
    for cid in sorted(set(canon.values())):
        u = units[cid]
        members = sorted(k for k, v in canon.items() if v == cid)
        detail = _unit_detail(conn, cid, members, units, sources_for[cid], caveats)
        _write(out_dir / "units" / f"{u['slug']}.json", detail)
        written += 1
        registry.append({
            "id": cid,
            "slug": u["slug"],
            "name": u["canonical_name"],
            "short_name": detail["facts"].get("short_name", {}).get("value"),
            "sources": sorted(sources_for[cid]),
            "source_count": len(sources_for[cid]),
            "lifecycle": u["lifecycle"],
            "positions": detail["counts"]["positions"],
            "current_officials": detail["counts"]["current_officials"],
            "children": detail["counts"]["children"],
        })

    edges = _edges(conn, canon)
    _write(out_dir / "units.json", {"units": registry})
    _write(out_dir / "edges.json", edges)
    _write(out_dir / "unresolved.json", _unresolved(conn, units, canon))
    _write(out_dir / "manifest.json", _manifest(conn, registry, edges, caveats))

    return {"units": written, "edges": len(edges["edges"]),
            "roots": len(edges["roots"]), "output": str(out_dir)}


# ── per-unit detail ────────────────────────────────────────────────────────
def _unit_detail(conn, cid, members, units, srcs, caveats) -> dict:
    marks = ",".join("?" * len(members))

    facts: dict[str, dict] = {}
    multi: dict[str, list] = {}
    contested: dict[str, list] = {}
    for r in conn.execute(
            f"SELECT * FROM statements WHERE unit_id IN ({marks}) AND valid_to IS NULL "
            f"ORDER BY predicate, value_key", members):
        # A fact inherits the confidence of the weakest link that reached it:
        # a published value attached through an adjudicated merge is only as
        # good as that merge.
        link = units[r["unit_id"]]["merge_method"] or "published"
        conf = min(r["confidence"], METHOD_CONFIDENCE.get(link, 1.0))
        item = {
            "value": r["value_raw"],
            "value_num": r["value_num"],
            "unit": r["value_unit"],
            "since": r["valid_from"][:10],
            # An observation date is not an effective date; the UI must say so.
            "effective_from": r["effective_from"],
            "provenance": {
                "source": r["source"], "source_url": r["source_url"],
                "locator": r["source_locator"], "method": r["method"],
                "link_method": link, "confidence": round(conf, 2),
                "band": band(conf), "observations": r["observations"],
                "cosmetic_revisions": r["cosmetic_revisions"],
            },
        }
        if r["value_key"]:
            multi.setdefault(r["predicate"], []).append(item)
            continue

        # Single-valued fact. Several sources may assert it, so precedence
        # decides which one is shown — NOT iteration order, which would make
        # the displayed value depend on a SQL row ordering. Federal Register
        # wins because it is the source of record for agency identity.
        prev = facts.get(r["predicate"])
        if prev is None or _src_rank(r["source"]) < _src_rank(prev["provenance"]["source"]):
            if prev is not None:
                contested.setdefault(r["predicate"], []).append(prev)
            facts[r["predicate"]] = item
        else:
            # Two sources disagreeing is a finding, not a bug to resolve away.
            if (prev["value"] or "").strip() != (item["value"] or "").strip():
                contested.setdefault(r["predicate"], []).append(item)

    officials = [dict(r) for r in conn.execute(
        f"SELECT pe.display_name AS person, po.title, po.appt_type, po.is_pas, "
        f"o.status, o.valid_from, o.valid_to, o.period_end, o.source, o.source_url "
        f"FROM occupancies o JOIN positions po ON po.id=o.position_id "
        f"JOIN persons pe ON pe.id=o.person_id "
        f"WHERE po.unit_id IN ({marks}) ORDER BY po.is_pas DESC, po.title", members)]

    n_pos = conn.execute(
        f"SELECT COUNT(*) c FROM positions WHERE unit_id IN ({marks})", members
    ).fetchone()["c"]
    n_children = conn.execute(
        f"SELECT COUNT(DISTINCT child_id) c FROM unit_edges WHERE parent_id IN ({marks}) "
        f"AND valid_to IS NULL", members).fetchone()["c"]

    history = [dict(r) for r in conn.execute(
        f"SELECT predicate, old_value, new_value, materiality, observed_at, "
        f"effective_from, source_url FROM changes WHERE unit_id IN ({marks}) "
        f"AND suppressed=0 ORDER BY observed_at DESC LIMIT 50", members)]

    return {
        "id": cid,
        "slug": units[cid]["slug"],
        "name": units[cid]["canonical_name"],
        "sources": sorted(srcs),
        "facts": facts,
        "multi_facts": multi,
        # Where sources disagree, shown rather than silently resolved.
        "contested": contested,
        "officials": [o for o in officials if o["status"] == "current"],
        "former_officials": [o for o in officials if o["status"] != "current"],
        "counts": {
            "positions": n_pos, "children": n_children,
            "current_officials": sum(1 for o in officials if o["status"] == "current"),
        },
        # Rendered next to any absence, so "0 positions" can never be read as
        # fact when the truth is "not covered by this source".
        "caveats": [c for c in caveats if c["source"] in srcs],
        "merged_from": [
            {"source": units[m]["anchor_source"], "key": units[m]["anchor_key"],
             "method": units[m]["merge_method"]}
            for m in members if m != cid
        ],
    }


# ── graph ──────────────────────────────────────────────────────────────────
def _edges(conn, canon) -> dict:
    """Emit the full edge list plus a spanning tree for default rendering.

    This is a DAG, not a tree: dual-hatted offices and matrixed units are real,
    and they are some of the most interesting cases. Non-tree edges are marked
    `secondary` so the UI can draw them as cross-links rather than drop them.
    """
    seen, edges = set(), []
    for r in conn.execute(
            "SELECT parent_id, child_id, source, edge_kind, valid_from, source_url "
            "FROM unit_edges WHERE valid_to IS NULL"):
        p = canon.get(r["parent_id"]) if r["parent_id"] else None
        c = canon.get(r["child_id"])
        if c is None or p == c:      # a merge can make an edge self-referential
            continue
        k = (p, c, r["edge_kind"], r["source"])
        if k in seen:
            continue
        seen.add(k)
        edges.append({"parent": p, "child": c, "kind": r["edge_kind"],
                      "source": r["source"], "since": r["valid_from"][:10],
                      "source_url": r["source_url"]})

    # Spanning tree: first parent wins, and every later claim is secondary.
    primary: dict[int, dict] = {}
    for e in sorted(edges, key=lambda e: (e["child"], e["source"], e["parent"] or 0)):
        e["secondary"] = e["child"] in primary
        if not e["secondary"] and e["parent"] is not None:
            primary[e["child"]] = e

    children = {c for c in primary}
    all_nodes = set(canon.values())
    roots = sorted(all_nodes - children)

    cycles = _find_cycles(primary)
    return {"edges": sorted(edges, key=lambda e: (e["child"], e["parent"] or 0)),
            "roots": roots, "cycles": cycles}


def _find_cycles(primary: dict[int, dict]) -> list[list[int]]:
    """A true cycle is excluded from the tree and REPORTED, never silently broken."""
    out, state = [], {}
    for start in primary:
        node, path = start, []
        while node is not None and node not in state:
            state[node] = "visiting"
            path.append(node)
            node = (primary.get(node) or {}).get("parent")
        if node is not None and state.get(node) == "visiting":
            out.append(path[path.index(node):])
        for n in path:
            state[n] = "done"
    return out


# ── supporting files ───────────────────────────────────────────────────────
def _unresolved(conn, units, canon) -> dict:
    """Shipped to the frontend on purpose.

    A UI showing 900 disconnected units looks like a working product; showing
    what has NOT been reconciled is what keeps that honest (plan §11 R3).
    """
    rows = []
    for r in conn.execute(
            "SELECT * FROM merge_candidates WHERE status='open' ORDER BY score DESC"):
        sig = json.loads(r["signals_json"])
        rows.append({
            "candidate": r["id"], "score": round(r["score"], 4),
            "left": {"slug": units[r["left_id"]]["slug"],
                     "name": sig["left"]["name"], "source": sig["left"]["source"]},
            "right": {"slug": units[r["right_id"]]["slug"],
                      "name": sig["right"]["name"], "source": sig["right"]["source"]},
            "name_exact": sig["name_exact"], "name_ratio": sig["name_ratio"],
        })
    return {"open_candidates": rows, "count": len(rows)}


def _caveats(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT source, facet, kind, summary, citation, citation_url, "
        "downgrades_absent FROM source_caveats ORDER BY source, facet")]


def _manifest(conn, registry, edges, caveats) -> dict:
    srcs = [dict(r) for r in conn.execute(
        "SELECT name, url, last_checked, last_changed, scope_claim, "
        "unchanged_streak, fail_streak FROM sources ORDER BY name")]
    total = len(registry) or 1
    multi3 = sum(1 for u in registry if u["source_count"] >= 3)
    multi2 = sum(1 for u in registry if u["source_count"] >= 2)
    return {
        "schema_version": conn.execute(
            "SELECT value FROM kv WHERE key='schema_version'").fetchone()["value"],
        "as_of": max((s["last_checked"] or "") for s in srcs) or None,
        "sources": srcs,
        "counts": {
            "units": len(registry), "edges": len(edges["edges"]),
            "roots": len(edges["roots"]), "cycles": len(edges["cycles"]),
            "positions": sum(u["positions"] for u in registry),
            "current_officials": sum(u["current_officials"] for u in registry),
        },
        "reconciliation": {
            "bound_to_2plus_sources": multi2,
            "bound_to_3plus_sources": multi3,
            "pct_3plus": round(100 * multi3 / total, 1),
            "note": ("headline metric; it FALLS when a bad merge is split, "
                     "which is honest in the direction that matters"),
        },
        "caveats": caveats,
        "history_note": ("Structural history begins at this project's first run. "
                         "Anything earlier is attestation, not history — the "
                         "sources publish only current state. Officeholder "
                         "history is the exception: OPM PLUM retains vacated "
                         "incumbencies, so tenure data predates run #1."),
    }


def _write(path: Path, obj) -> None:
    """Deterministic: sorted keys, stable separators, trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=True,
                               ensure_ascii=False, default=str) + "\n")
