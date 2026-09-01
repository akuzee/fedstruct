"""Identity reconciliation tests (plan §2, §4).

The first test in this file is the most important one in the project. It
encodes the rule the whole design rests on — name similarity produces
candidates, never merges — as an executable guard, so the rule cannot be
loosened by accident later.
"""

from __future__ import annotations

import json

import pytest

from src import resolve

NOW = "2026-09-01T00:00:00+00:00"


def _adjudications(env, doc: str):
    p = env.root / "adjudications.yaml"
    p.write_text(doc)
    return p


# ── the central guard ──────────────────────────────────────────────────────
def test_identical_names_without_a_shared_identifier_do_not_merge(ingested):
    """A false merge invents edges and is not visible by inspection.

    PLUM publishes no identifiers at all — only uppercase free text — so its
    units may never bind automatically, however exactly the names agree.
    """
    resolve.propose(ingested.conn, NOW)
    resolve.apply_auto(ingested.conn, NOW)

    plum_epa = ingested.conn.execute(
        "SELECT id, merged_into FROM units WHERE anchor_source='plum' "
        "AND name_norm='environmental protection agency'").fetchone()
    if plum_epa is None:
        pytest.skip("fixture has no PLUM EPA row")

    assert plum_epa["merged_into"] is None, \
        "a PLUM unit bound on name evidence alone — the one thing that must never happen"

    # It must still be VISIBLE as unfinished work, not silently dropped.
    assert ingested.one(
        "SELECT COUNT(*) FROM merge_candidates WHERE status='open' "
        "AND (left_id=? OR right_id=?)", plum_epa["id"], plum_epa["id"]) >= 1


def test_exact_slug_match_does_auto_bind(ingested):
    """Two independently-published government slugs agreeing 1:1 is an identifier."""
    resolve.propose(ingested.conn, NOW)
    res = resolve.apply_auto(ingested.conn, NOW)

    assert res.auto_bound >= 1
    row = ingested.conn.execute(
        "SELECT merge_method, merge_evidence FROM units "
        "WHERE anchor_source='ecfr_agencies' AND merged_into IS NOT NULL "
        "LIMIT 1").fetchone()
    assert row["merge_method"] == "crosswalk"
    assert "slug" in row["merge_evidence"]


def test_federal_register_wins_as_the_survivor(ingested):
    """FR anchors the survivor: it is the only source publishing hierarchy."""
    resolve.propose(ingested.conn, NOW)
    resolve.apply_auto(ingested.conn, NOW)

    for r in ingested.q("SELECT merged_into FROM units WHERE merged_into IS NOT NULL"):
        survivor = ingested.one("SELECT anchor_source FROM units WHERE id=?", r[0])
        assert survivor == "fr_agencies"


def test_never_merge_beats_a_perfect_score(ingested):
    """Anti-merges are consulted before scoring; nothing overrides them."""
    a = ingested.conn.execute(
        "SELECT id, anchor_source, anchor_key FROM units "
        "WHERE anchor_source='fr_agencies' AND name_norm='environmental protection agency'"
    ).fetchone()
    b = ingested.conn.execute(
        "SELECT id, anchor_source, anchor_key FROM units "
        "WHERE anchor_source='ecfr_agencies' AND name_norm='environmental protection agency'"
    ).fetchone()
    if not (a and b):
        pytest.skip("fixture lacks an EPA pair")

    path = _adjudications(ingested, f"""
never_merge:
  - pair: ["{a['anchor_source']}:{a['anchor_key']}", "{b['anchor_source']}:{b['anchor_key']}"]
    reason: "test anti-merge"
    decided_by: test
    decided_on: 2026-09-01
""")
    resolve.apply_adjudications(ingested.conn, path, NOW)
    r = resolve.propose(ingested.conn, NOW)
    resolve.apply_auto(ingested.conn, NOW)

    assert r.blocked_by_never_merge >= 1
    assert ingested.one("SELECT merged_into FROM units WHERE id=?", b["id"]) is None


# ── reversibility ──────────────────────────────────────────────────────────
def test_deleting_a_merge_entry_unmerges_on_rerun(ingested):
    a = ingested.conn.execute(
        "SELECT anchor_source, anchor_key, id FROM units WHERE anchor_source='fr_agencies' "
        "AND name_norm='environmental protection agency'").fetchone()
    b = ingested.conn.execute(
        "SELECT anchor_source, anchor_key, id FROM units WHERE anchor_source='plum' "
        "AND name_norm='environmental protection agency'").fetchone()
    if not (a and b):
        pytest.skip("fixture lacks the pair")

    path = _adjudications(ingested, f"""
merges:
  - canonical: {a['anchor_source']}:{a['anchor_key']}
    absorbs: ["{b['anchor_source']}:{b['anchor_key']}"]
    evidence: "test merge"
    decided_by: test
    decided_on: 2026-09-01
""")
    resolve.apply_adjudications(ingested.conn, path, NOW)
    assert ingested.one("SELECT merged_into FROM units WHERE id=?", b["id"]) == a["id"]

    # Delete the entry and re-run: the merge must be gone.
    path.write_text("merges: []\n")
    resolve.apply_adjudications(ingested.conn, path, NOW)
    assert ingested.one("SELECT merged_into FROM units WHERE id=?", b["id"]) is None


def test_a_rule_rerun_does_not_clear_a_human_decision(ingested):
    """Rules may un-bind what rules bound; only a human un-binds a human."""
    a = ingested.conn.execute(
        "SELECT anchor_source, anchor_key, id FROM units WHERE anchor_source='fr_agencies' "
        "AND name_norm='environmental protection agency'").fetchone()
    b = ingested.conn.execute(
        "SELECT anchor_source, anchor_key, id FROM units WHERE anchor_source='plum' "
        "AND name_norm='environmental protection agency'").fetchone()
    if not (a and b):
        pytest.skip("fixture lacks the pair")

    path = _adjudications(ingested, f"""
merges:
  - canonical: {a['anchor_source']}:{a['anchor_key']}
    absorbs: ["{b['anchor_source']}:{b['anchor_key']}"]
    evidence: "human call"
    decided_by: test
    decided_on: 2026-09-01
""")
    resolve.apply_adjudications(ingested.conn, path, NOW)
    resolve.propose(ingested.conn, NOW)
    resolve.apply_auto(ingested.conn, NOW)
    resolve.apply_adjudications(ingested.conn, path, NOW)

    assert ingested.one("SELECT merged_into FROM units WHERE id=?", b["id"]) == a["id"]
    assert ingested.one("SELECT merge_method FROM units WHERE id=?", b["id"]) == "adjudicated"


def test_resolve_is_idempotent(ingested):
    resolve.propose(ingested.conn, NOW)
    resolve.apply_auto(ingested.conn, NOW)
    first = ingested.q("SELECT id, merged_into, merge_method FROM units ORDER BY id")

    resolve.propose(ingested.conn, "2026-10-01T00:00:00+00:00")
    resolve.apply_auto(ingested.conn, "2026-10-01T00:00:00+00:00")
    second = ingested.q("SELECT id, merged_into, merge_method FROM units ORDER BY id")

    assert [tuple(r) for r in first] == [tuple(r) for r in second]


def test_a_stale_reference_is_recorded_not_raised(ingested):
    """One bad YAML line must not stop every other decision from applying."""
    path = _adjudications(ingested, """
merges:
  - canonical: fr_agencies:99999
    absorbs: ["plum:does-not-exist"]
    evidence: "dangling"
    decided_by: test
    decided_on: 2026-09-01
""")
    res = resolve.apply_adjudications(ingested.conn, path, NOW)

    assert res.errors and "no unit matches" in res.errors[0]
    err = ingested.one("SELECT apply_error FROM adjudications WHERE kind='merge'")
    assert err and "no unit matches" in err


# ── merges never rewrite facts ─────────────────────────────────────────────
def test_merging_does_not_touch_statements(ingested):
    before = [tuple(r) for r in ingested.q(
        "SELECT id, unit_id, predicate, value_norm FROM statements ORDER BY id")]

    resolve.propose(ingested.conn, NOW)
    resolve.apply_auto(ingested.conn, NOW)

    after = [tuple(r) for r in ingested.q(
        "SELECT id, unit_id, predicate, value_norm FROM statements ORDER BY id")]
    assert before == after, "a merge rewrote facts; it must only move a pointer"


def test_canonical_view_follows_merge_pointers(ingested):
    resolve.propose(ingested.conn, NOW)
    resolve.apply_auto(ingested.conn, NOW)

    merged = ingested.conn.execute(
        "SELECT id, merged_into FROM units WHERE merged_into IS NOT NULL LIMIT 1"
    ).fetchone()
    if merged is None:
        pytest.skip("nothing merged in this fixture")
    assert ingested.one(
        "SELECT canonical_id FROM v_canonical WHERE id=?", merged["id"]
    ) == merged["merged_into"]


def test_export_yaml_writes_nothing_to_disk(ingested):
    """The tool drafts; the human commits."""
    resolve.propose(ingested.conn, NOW)
    cand = ingested.one("SELECT id FROM merge_candidates ORDER BY score DESC LIMIT 1")
    if cand is None:
        pytest.skip("no candidates")

    listing = set(p.name for p in ingested.root.iterdir())
    out = resolve.export_yaml(ingested.conn, cand)

    assert "merges:" in out and "decided_by:" in out
    assert set(p.name for p in ingested.root.iterdir()) == listing
