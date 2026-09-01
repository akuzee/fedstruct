"""Output-layer tests (plan §7, §10).

Two invariants the product's credibility rests on: the build is a pure function
of the database, and no fact reaches a reader without a citation.
"""

from __future__ import annotations

import json

from src import resolve, views

NOW = "2026-09-01T00:00:00+00:00"


def _build(env):
    out = env.root / "output"
    views.build(env.conn, out)
    return out


def test_build_is_byte_identical_on_rerun(ingested):
    """"Output files are pure derived views" is testable, so test it."""
    out = _build(ingested)
    first = {p.name: p.read_bytes() for p in sorted(out.rglob("*.json"))}
    views.build(ingested.conn, out)
    second = {p.name: p.read_bytes() for p in sorted(out.rglob("*.json"))}
    assert first == second


def test_every_displayed_fact_carries_a_clickable_source(ingested):
    out = _build(ingested)
    for path in (out / "units").glob("*.json"):
        d = json.loads(path.read_text())
        for pred, fact in d["facts"].items():
            p = fact["provenance"]
            assert p["source_url"], f"{path.name}:{pred} has no source_url"
            assert p["source"], f"{path.name}:{pred} has no source"
            assert p["band"] in ("published", "linked", "inferred")
        for pred, items in d["multi_facts"].items():
            for it in items:
                assert it["provenance"]["source_url"], f"{path.name}:{pred}"


def test_confidence_is_the_weakest_link_not_the_average(ingested):
    """A published value reached through a weaker join is only as good as the join."""
    resolve.propose(ingested.conn, NOW)
    resolve.apply_auto(ingested.conn, NOW)
    out = _build(ingested)

    merged = ingested.conn.execute(
        "SELECT s.slug FROM units u JOIN units s ON s.id = u.merged_into "
        "WHERE u.merge_method='crosswalk' LIMIT 1").fetchone()
    if merged is None:
        return
    d = json.loads((out / "units" / f"{merged['slug']}.json").read_text())
    for fact in d["facts"].values():
        p = fact["provenance"]
        if p["link_method"] == "crosswalk":
            assert p["confidence"] <= views.METHOD_CONFIDENCE["crosswalk"]


def test_federal_register_wins_a_single_valued_fact(ingested):
    """Display must not depend on SQL row ordering."""
    resolve.propose(ingested.conn, NOW)
    resolve.apply_auto(ingested.conn, NOW)
    out = _build(ingested)

    for path in (out / "units").glob("*.json"):
        d = json.loads(path.read_text())
        name = d["facts"].get("name")
        if name and "fr_agencies" in d["sources"]:
            assert name["provenance"]["source"] == "fr_agencies", path.name


def test_unresolved_candidates_are_shipped_to_the_reader(ingested):
    """A UI showing only what reconciled looks finished when it isn't."""
    resolve.propose(ingested.conn, NOW)
    out = _build(ingested)
    d = json.loads((out / "unresolved.json").read_text())
    assert d["count"] >= 1
    assert d["open_candidates"][0]["left"]["slug"]


def test_caveats_reach_the_unit_page(ingested):
    """GAO's finding about PLUM must render next to a PLUM absence."""
    ingested.conn.execute(
        "INSERT INTO source_caveats (source, facet, kind, summary, citation, "
        "citation_url, downgrades_absent) VALUES "
        "('plum','leadership','incompleteness','PLUM omits entities.',"
        "'GAO-26-108164','https://www.gao.gov/products/gao-26-108164',1)")
    ingested.conn.commit()
    out = _build(ingested)

    hit = [json.loads(p.read_text()) for p in (out / "units").glob("*.json")]
    plum_units = [d for d in hit if "plum" in d["sources"]]
    assert plum_units, "fixture has no PLUM unit"
    assert any(c["citation"] == "GAO-26-108164"
               for d in plum_units for c in d["caveats"])


def test_secondary_edges_are_marked_not_dropped(ingested):
    """This is a DAG. Dual-hatted offices must survive as cross-links."""
    out = _build(ingested)
    e = json.loads((out / "edges.json").read_text())
    assert all("secondary" in x for x in e["edges"])
    # Every child appears at most once in the spanning tree.
    primary = [x["child"] for x in e["edges"] if not x["secondary"] and x["parent"]]
    assert len(primary) == len(set(primary))


def test_no_cycles_and_cycles_are_reported_when_present(ingested):
    out = _build(ingested)
    e = json.loads((out / "edges.json").read_text())
    assert e["cycles"] == []

    # Inject a cycle and confirm it is detected rather than silently broken.
    rows = ingested.q(
        "SELECT id, parent_id, child_id FROM unit_edges WHERE parent_id IS NOT NULL LIMIT 1")
    if not rows:
        return
    r = rows[0]
    ingested.conn.execute(
        "UPDATE unit_edges SET parent_id=? WHERE child_id=? AND valid_to IS NULL",
        (r["child_id"], r["parent_id"]))
    ingested.conn.commit()
    e2 = json.loads((_build(ingested) / "edges.json").read_text())
    assert e2["cycles"], "a cycle was silently broken instead of reported"


def test_manifest_states_the_history_limitation(ingested):
    """The README claim must also travel with the data."""
    out = _build(ingested)
    m = json.loads((out / "manifest.json").read_text())
    assert "first run" in m["history_note"]
    assert m["reconciliation"]["bound_to_2plus_sources"] >= 0
    assert m["as_of"]
