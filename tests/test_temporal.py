"""The load-bearing tests: idempotency, the gate, and change classification.

If any of these regress, the project is quietly producing wrong history, which
is worse than producing none.
"""

from __future__ import annotations

import json

import pytest

from src import db, sources
from src.db import SchemaVersionMismatch
from src.http import SourceBlocked
from src.ingest import ingest


# ── idempotency (plan §10) ─────────────────────────────────────────────────
def test_second_run_opens_and_closes_nothing(ingested):
    """Run `all` twice over identical bytes: only observation counters move."""
    before = ingested.q(
        "SELECT id, valid_from, valid_to, value_norm FROM statements ORDER BY id")
    res = ingested.run_all(now="2026-10-01T00:00:00+00:00")

    for name, r in res.items():
        # Second fetch hash-matches, so there is nothing to parse at all.
        assert getattr(r, "outcome", None) == "unchanged", f"{name}: {r}"

    after = ingested.q(
        "SELECT id, valid_from, valid_to, value_norm FROM statements ORDER BY id")
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    assert ingested.one("SELECT COUNT(*) FROM changes") == 0


def test_reingesting_same_payload_is_a_no_op(ingested):
    """Even bypassing the gate, applying identical records changes nothing."""
    src = sources.get("fr_agencies")
    parsed = src.parse(ingested.sess.bodies["federalregister.gov"])
    run_id = db.start_run(ingested.conn, "test", "2026-10-01T00:00:00+00:00")
    fetch_id = ingested.one("SELECT id FROM fetches WHERE source='fr_agencies' LIMIT 1")

    res = ingest(ingested.conn, src, parsed, run_id=run_id, fetch_id=fetch_id,
                 now="2026-10-01T00:00:00+00:00", max_changes=40)

    assert res.statements_opened == 0
    assert res.statements_closed == 0
    assert res.units_created == 0
    assert res.changes == 0


# ── the sha256 gate (plan §6) ──────────────────────────────────────────────
def test_unchanged_source_costs_one_request_and_no_parse(ingested):
    ingested.sess.request_count = 0
    stmts_before = ingested.one("SELECT COUNT(*) FROM statements")

    ingested.run_all(now="2026-10-01T00:00:00+00:00")

    assert ingested.sess.request_count == 3          # one per source, no more
    assert ingested.one("SELECT COUNT(*) FROM statements") == stmts_before
    # No body was written for an unchanged fetch.
    assert ingested.one(
        "SELECT COUNT(*) FROM fetches WHERE outcome='unchanged' "
        "AND raw_path IS NOT NULL") == 0


def test_unchanged_streak_widens_the_interval(ingested):
    from datetime import date, timedelta

    from src.config import BACKOFF_INTERVAL_DAYS, UNCHANGED_STREAK_THRESHOLD
    from src.fetch import interval_for

    # Each run must actually be DUE, or the streak never advances — step by the
    # cadence, not by an arbitrary interval.
    day = date(2026, 9, 1)
    for _ in range(UNCHANGED_STREAK_THRESHOLD):
        day += timedelta(days=31)
        ingested.run_all(now=f"{day.isoformat()}T00:00:00+00:00")

    src = ingested.conn.execute(
        "SELECT * FROM sources WHERE name='fr_agencies'").fetchone()
    assert src["unchanged_streak"] >= UNCHANGED_STREAK_THRESHOLD
    assert interval_for(src, src["cadence_days"]) == BACKOFF_INTERVAL_DAYS


# ── change classification (plan §6) ────────────────────────────────────────
def test_cosmetic_change_opens_no_interval_and_makes_no_news(ingested):
    """A reworded description must not become a diff at all."""
    data = json.loads(ingested.sess.bodies["federalregister.gov"])
    data[0]["description"] = "Completely rewritten prose that means the same thing."
    ingested.sess.set_body("federalregister.gov", json.dumps(data).encode())

    ingested.run_all(now="2026-10-01T00:00:00+00:00")

    assert ingested.one(
        "SELECT COUNT(*) FROM statements WHERE predicate='description' "
        "AND valid_to IS NOT NULL") == 0, "description must never open an interval"
    assert ingested.one(
        "SELECT COUNT(*) FROM changes WHERE suppressed=0") == 0
    assert ingested.one(
        "SELECT COUNT(*) FROM changes WHERE suppressed=1") >= 1


def test_capitalization_fix_is_cosmetic_not_a_rename(ingested):
    """normalize_name collapses case, so this is not news."""
    data = json.loads(ingested.sess.bodies["federalregister.gov"])
    target = next(a for a in data if a["slug"] == "transportation-department")
    target["name"] = target["name"].upper()
    ingested.sess.set_body("federalregister.gov", json.dumps(data).encode())

    ingested.run_all(now="2026-10-01T00:00:00+00:00")

    assert ingested.one(
        "SELECT COUNT(*) FROM changes WHERE predicate='name' AND suppressed=0") == 0


def test_real_rename_closes_the_interval_and_is_structural(ingested):
    data = json.loads(ingested.sess.bodies["federalregister.gov"])
    target = next(a for a in data if a["slug"] == "transportation-department")
    target["name"] = "Department of Transit and Mobility"
    ingested.sess.set_body("federalregister.gov", json.dumps(data).encode())

    ingested.run_all(now="2026-10-01T00:00:00+00:00")

    closed = ingested.q(
        "SELECT valid_to FROM statements WHERE predicate='name' AND valid_to IS NOT NULL")
    assert len(closed) == 1
    row = ingested.conn.execute(
        "SELECT * FROM changes WHERE predicate='name' AND suppressed=0").fetchone()
    assert row["materiality"] == "structural"
    # The observation date is NOT an effective date, and the renderer depends
    # on that distinction being explicit (plan §11 R7).
    assert row["effective_from"] is None
    assert row["observed_at"] == "2026-10-01T00:00:00+00:00"


def test_parent_change_closes_the_edge(ingested):
    data = json.loads(ingested.sess.bodies["federalregister.gov"])
    faa = next(a for a in data if a["slug"] == "federal-aviation-administration")
    epa = next(a for a in data if a["slug"] == "environmental-protection-agency")
    faa["parent_id"] = epa["id"]
    ingested.sess.set_body("federalregister.gov", json.dumps(data).encode())

    ingested.run_all(now="2026-10-01T00:00:00+00:00")

    assert ingested.one(
        "SELECT COUNT(*) FROM unit_edges WHERE valid_to IS NOT NULL") == 1
    new_parent = ingested.one(
        "SELECT p.slug FROM unit_edges e JOIN units c ON c.id=e.child_id "
        "JOIN units p ON p.id=e.parent_id WHERE c.slug LIKE 'federal-aviation%' "
        "AND e.valid_to IS NULL")
    assert new_parent == "environmental-protection-agency"


# ── multi-valued predicates ────────────────────────────────────────────────
def test_multiple_cfr_chapters_coexist(ingested):
    """EPA does not have "a" CFR chapter; it has several, each its own fact."""
    n = ingested.one(
        "SELECT COUNT(*) FROM statements s JOIN units u ON u.id=s.unit_id "
        "WHERE s.predicate='cfr_chapter' AND s.valid_to IS NULL "
        "AND u.anchor_source='ecfr_agencies' "
        "AND u.name_norm LIKE '%environmental protection%'")
    assert n > 1, "multi-valued predicate collapsed to one row"


def test_losing_a_chapter_closes_only_that_chapter(ingested):
    payload = json.loads(ingested.sess.bodies["ecfr.gov"])
    epa = next(a for a in payload["agencies"]
               if a["slug"] == "environmental-protection-agency")
    before = len(epa["cfr_references"])
    dropped = epa["cfr_references"].pop()
    ingested.sess.set_body("ecfr.gov", json.dumps(payload).encode())

    ingested.run_all(now="2026-10-01T00:00:00+00:00")

    assert ingested.one(
        "SELECT COUNT(*) FROM statements WHERE predicate='cfr_chapter' "
        "AND valid_to IS NOT NULL") == 1
    still_open = ingested.one(
        "SELECT COUNT(*) FROM statements s JOIN units u ON u.id=s.unit_id "
        "WHERE s.predicate='cfr_chapter' AND s.valid_to IS NULL "
        "AND u.anchor_source='ecfr_agencies' "
        "AND u.name_norm LIKE '%environmental protection%'")
    assert still_open == before - 1


# ── circuit breakers (plan §11 R1) ─────────────────────────────────────────
def test_mass_change_quarantines_the_run_and_commits_nothing(ingested):
    """Identity drift must never publish 400 phantom reorganizations."""
    import dataclasses

    # The fixture holds 9 agencies, so scale the breaker to it. What is under
    # test is the ratio-independent rule: too many structural changes at once
    # commits nothing at all.
    ingested.cfg = dataclasses.replace(ingested.cfg, max_changes_per_run=3)

    data = json.loads(ingested.sess.bodies["federalregister.gov"])
    for a in data:
        a["name"] = "Renamed " + a["name"]
    ingested.sess.set_body("federalregister.gov", json.dumps(data).encode())

    before = ingested.one("SELECT COUNT(*) FROM statements WHERE valid_to IS NOT NULL")
    res = ingested.run_all(now="2026-10-01T00:00:00+00:00")

    assert res["fr_agencies"].quarantined
    assert "max_changes_per_run" in res["fr_agencies"].reason
    assert ingested.one(
        "SELECT COUNT(*) FROM statements WHERE valid_to IS NOT NULL") == before
    assert ingested.one("SELECT COUNT(*) FROM changes") == 0


def test_truncated_payload_is_quarantined_not_ingested(ingested):
    """A truncated response and a mass abolition look identical to a differ."""
    data = json.loads(ingested.sess.bodies["federalregister.gov"])
    ingested.sess.set_body("federalregister.gov", json.dumps(data[:2]).encode())

    res = ingested.run_all(now="2026-10-01T00:00:00+00:00")

    assert isinstance(res["fr_agencies"], str) and "row count" in res["fr_agencies"]
    assert ingested.one(
        "SELECT outcome FROM fetches WHERE source='fr_agencies' "
        "ORDER BY id DESC LIMIT 1") == "quarantined"


# ── blocking must never look like absence (plan §11 R5) ────────────────────
def test_block_is_a_failure_not_an_empty_result(env):
    env.sess.raise_for["federalregister.gov"] = SourceBlocked("redirected to unblock page")

    res = env.run_all()

    assert res["fr_agencies"].outcome == "failed"
    assert env.one("SELECT COUNT(*) FROM units WHERE anchor_source='fr_agencies'") == 0
    assert env.one(
        "SELECT fail_streak FROM sources WHERE name='fr_agencies'") == 1
    # Critically: not recorded as 'unchanged', which would have silently
    # widened the polling interval on a source we cannot actually reach.
    assert env.one(
        "SELECT outcome FROM fetches WHERE source='fr_agencies'") == "failed"


def test_block_page_served_as_http_200_never_becomes_data(env):
    """Akamai serves its block page as HTTP 200 (verified on GitHub runners).

    The sniff must reject it at fetch time — and reject it AGAIN on the next
    identical response. If the block page were stored, its stable hash would
    make the next fetch read 'unchanged' and the outage would go invisible.
    """
    block = b"<html><head><title>Access Denied</title></head></html>"
    env.sess.set_body("escs.opm.gov", block)

    first = env.run_all()
    assert first["plum"].outcome == "failed"
    assert "not this source's data" in first["plum"].error

    second = env.run_all(force=True, now="2026-10-01T00:00:00+00:00")
    assert second["plum"].outcome == "failed", \
        "an identical block page hash-matched and read as 'unchanged'"

    assert env.one("SELECT COUNT(*) FROM units WHERE anchor_source='plum'") == 0
    assert env.one("SELECT fail_streak FROM sources WHERE name='plum'") == 2
    # And the healthy sources were refreshed despite the blocked one.
    assert env.one("SELECT COUNT(*) FROM units WHERE anchor_source='fr_agencies'") > 0


def test_empty_agency_list_is_refused(env):
    env.sess.set_body("federalregister.gov", b"[]")
    with pytest.raises(ValueError, match="empty"):
        sources.get("fr_agencies").parse(b"[]")


# ── schema guard ───────────────────────────────────────────────────────────
def test_schema_version_mismatch_raises(env, monkeypatch):
    env.conn.execute("UPDATE kv SET value='99' WHERE key='schema_version'")
    env.conn.commit()
    env.conn.close()
    with pytest.raises(SchemaVersionMismatch):
        db.connect(env.cfg.db_path)
