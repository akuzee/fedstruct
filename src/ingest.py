"""Applying parsed records to the ledger as validity intervals (plan §3, §6).

The whole temporal model lives here. For each (unit, predicate, source) there
is at most one OPEN row — enforced by a partial unique index, so a bug becomes
an IntegrityError at write time rather than a silently wrong page.

Comparison is on `value_norm`, never `value_raw`. A capitalization fix or a
reworded description bumps counters in place and produces zero rows and zero
changelog lines. That is what keeps growth proportional to news.

Idempotency: running this twice over identical bytes opens nothing, closes
nothing, and emits no changes — only `observations` and `last_fetch_id` move.
"""

from __future__ import annotations

from dataclasses import dataclass

from .normalize import (
    MULTIVALUED,
    is_versioned,
    materiality,
    normalize_name,
    normalize_value,
    slugify,
    value_key,
)
from .sources.base import Parsed, Source

RULES_VERSION = "1"


@dataclass
class IngestResult:
    source: str
    units_created: int = 0
    statements_opened: int = 0
    statements_closed: int = 0
    cosmetic: int = 0
    edges_opened: int = 0
    edges_closed: int = 0
    positions_created: int = 0
    occupancies_created: int = 0
    changes: int = 0
    suppressed: int = 0
    quarantined: bool = False
    reason: str | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (0, None, False)} \
            or {"source": self.source, "no_changes": True}


def ingest(conn, source: Source, parsed: Parsed, *, run_id: int, fetch_id: int,
           now: str, max_changes: int) -> IngestResult:
    """Apply one source's parse. Single transaction: all of it, or none of it."""
    source.check_allowed(parsed)
    res = IngestResult(source=source.name)
    pending_changes: list[tuple] = []

    try:
        conn.execute("BEGIN")

        key_to_id: dict[str, int] = {}
        for u in parsed.units:
            uid, created = _upsert_unit(conn, source, u, run_id, now, fetch_id)
            key_to_id[u.anchor_key] = uid
            res.units_created += int(created)

        # Track which multi-valued facts this fetch asserted, so the sweep
        # below can close the ones that vanished (a lost CFR chapter).
        seen_multi: set[tuple[int, str, str]] = set()

        for s in parsed.statements:
            uid = key_to_id.get(s.unit_anchor_key)
            if uid is None:
                continue
            outcome, vkey = _apply_statement(conn, source, s, uid, run_id, fetch_id, now)
            if s.predicate in MULTIVALUED:
                seen_multi.add((uid, s.predicate, vkey))
            if outcome == "opened":
                res.statements_opened += 1
            elif outcome == "changed":
                res.statements_opened += 1
                res.statements_closed += 1
            elif outcome == "cosmetic":
                res.cosmetic += 1

            if outcome in ("changed", "cosmetic"):
                pending_changes.append(
                    _change_row(conn, uid, s, outcome, run_id, fetch_id, now))

        # A multi-valued fact is retracted by ABSENCE, not by a new value, so
        # it needs a sweep. Scoped to units this fetch actually mentioned —
        # otherwise a source that stopped listing a unit entirely would look
        # like that unit losing every chapter at once.
        res.statements_closed += _close_vanished_multi(
            conn, source, key_to_id, seen_multi, run_id, now)

        if source.publishes_hierarchy:
            o, c = _apply_edges(conn, source, parsed, key_to_id, run_id, fetch_id, now)
            res.edges_opened, res.edges_closed = o, c

        pos_ids: dict[str, int] = {}
        for p in parsed.positions:
            uid = key_to_id.get(p.unit_anchor_key)
            if uid is None:
                continue
            pid, created = _upsert_position(conn, source, p, uid, now)
            pos_ids[p.anchor_key] = pid
            res.positions_created += int(created)

        for occ in parsed.occupancies:
            pid = pos_ids.get(occ.position_anchor_key)
            if pid is None:
                continue
            res.occupancies_created += int(
                _upsert_occupancy(conn, source, occ, pid, fetch_id, now))

        # ── the circuit breaker (plan §11 R1) ──────────────────────────────
        # Identity drift — FR reassigning ids, PLUM rewording its uppercase
        # strings — presents as hundreds of simultaneous "reorganizations".
        # Published once, that destroys the project's credibility permanently.
        # So an implausible run commits nothing at all.
        structural = [c for c in pending_changes if c[4] == "structural"]
        if len(structural) > max_changes:
            conn.execute("ROLLBACK")
            res.quarantined = True
            res.reason = (
                f"{len(structural)} structural changes exceeds "
                f"max_changes_per_run={max_changes}; run quarantined, nothing "
                f"committed. Inspect before re-running with --force."
            )
            return res

        for row in pending_changes:
            _insert_change(conn, row)
            if row[5]:
                res.suppressed += 1
            else:
                res.changes += 1

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return res


# ── units ──────────────────────────────────────────────────────────────────
def _upsert_unit(conn, source: Source, u, run_id: int, now: str, fetch_id: int):
    row = conn.execute(
        "SELECT id FROM units WHERE anchor_source = ? AND anchor_key = ?",
        (source.name, u.anchor_key),
    ).fetchone()

    if row:
        uid = int(row["id"])
        conn.execute(
            "UPDATE units SET last_seen_run = ?, absent_runs = 0, "
            "lifecycle = CASE WHEN lifecycle = 'absent_from_source' "
            "THEN 'active' ELSE lifecycle END WHERE id = ?",
            (run_id, uid),
        )
    else:
        slug = _unique_slug(conn, u.slug_hint or slugify(u.name))
        cur = conn.execute(
            "INSERT INTO units (slug, canonical_name, name_norm, branch, kind, "
            "anchor_source, anchor_key, first_seen_run, last_seen_run, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (slug, u.name, normalize_name(u.name), u.branch, u.kind,
             source.name, u.anchor_key, run_id, run_id, now),
        )
        uid = int(cur.lastrowid)

    # The crosswalk. PK (scheme, key) forbids one external id fanning out
    # across two units; a conflict means genuine identity drift and is left for
    # adjudication rather than silently rebound.
    for scheme, key in (u.extra_keys or {}).items():
        conn.execute(
            "INSERT INTO unit_keys (scheme, key, unit_id, method, confidence, fetch_id) "
            "VALUES (?,?,?,'published',1.0,?) ON CONFLICT(scheme, key) DO NOTHING",
            (scheme, key, uid, fetch_id),
        )
    return uid, not row


def _unique_slug(conn, base: str) -> str:
    slug, n = base, 2
    while conn.execute("SELECT 1 FROM units WHERE slug = ?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    return slug


# ── statements ─────────────────────────────────────────────────────────────
def _apply_statement(conn, source: Source, s, uid: int, run_id: int,
                     fetch_id: int, now: str) -> tuple[str, str]:
    norm = normalize_value(s.predicate, s.value)
    raw = s.value
    if s.value_num is not None and norm is None:
        norm = f"{s.value_num:.6g}"
        raw = norm

    vkey = value_key(s.predicate, norm)
    cur = conn.execute(
        "SELECT * FROM statements WHERE unit_id = ? AND predicate = ? "
        "AND source = ? AND value_key = ? AND valid_to IS NULL",
        (uid, s.predicate, source.name, vkey),
    ).fetchone()

    if cur is None:
        _open_statement(conn, source, s, uid, norm, raw, vkey, run_id, fetch_id, now)
        return "opened", vkey

    if cur["value_norm"] == norm:
        # Unchanged where it counts. If the raw text drifted, record that as a
        # cosmetic revision in place — no new row, no changelog line.
        if cur["value_raw"] != raw:
            conn.execute(
                "UPDATE statements SET value_raw = ?, cosmetic_revisions = "
                "cosmetic_revisions + 1, last_fetch_id = ?, observations = "
                "observations + 1 WHERE id = ?",
                (raw, fetch_id, cur["id"]),
            )
            return "cosmetic", vkey
        conn.execute(
            "UPDATE statements SET last_fetch_id = ?, observations = observations + 1 "
            "WHERE id = ?",
            (fetch_id, cur["id"]),
        )
        return "unchanged", vkey

    if not is_versioned(s.predicate):
        # Cosmetic predicates never open an interval, however much they move.
        conn.execute(
            "UPDATE statements SET value_raw = ?, value_norm = ?, "
            "cosmetic_revisions = cosmetic_revisions + 1, last_fetch_id = ?, "
            "observations = observations + 1 WHERE id = ?",
            (raw, norm, fetch_id, cur["id"]),
        )
        return "cosmetic", vkey

    conn.execute(
        "UPDATE statements SET valid_to = ?, valid_to_run = ? WHERE id = ?",
        (now, run_id, cur["id"]),
    )
    _open_statement(conn, source, s, uid, norm, raw, vkey, run_id, fetch_id, now)
    return "changed", vkey


def _close_vanished_multi(conn, source: Source, key_to_id: dict,
                          seen: set, run_id: int, now: str) -> int:
    """Close multi-valued facts the source stopped asserting for a unit."""
    if not key_to_id:
        return 0
    closed = 0
    for pred in MULTIVALUED:
        rows = conn.execute(
            f"SELECT id, unit_id, value_key FROM statements WHERE source = ? "
            f"AND predicate = ? AND valid_to IS NULL AND unit_id IN "
            f"({','.join('?' * len(key_to_id))})",
            (source.name, pred, *key_to_id.values()),
        ).fetchall()
        for r in rows:
            if (r["unit_id"], pred, r["value_key"]) not in seen:
                conn.execute(
                    "UPDATE statements SET valid_to = ?, valid_to_run = ? WHERE id = ?",
                    (now, run_id, r["id"]))
                closed += 1
    return closed


def _open_statement(conn, source, s, uid, norm, raw, vkey, run_id, fetch_id, now) -> None:
    import json

    conn.execute(
        "INSERT INTO statements (unit_id, predicate, value_key, value_norm, "
        "value_raw, value_num, value_unit, source, method, confidence, valid_from, "
        "valid_from_run, valid_precision, effective_from, period_start, "
        "period_end, open_fetch_id, last_fetch_id, source_url, source_locator, "
        "raw_hash, qualifiers_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, s.predicate, vkey, norm, raw, s.value_num, s.value_unit, source.name,
         s.method, s.confidence, now, run_id, s.valid_precision, s.effective_from,
         s.period_start, s.period_end, fetch_id, fetch_id, s.source_url,
         s.source_locator, _hash(raw),
         json.dumps(s.qualifiers) if s.qualifiers else None),
    )


def _hash(v) -> str | None:
    import hashlib

    return hashlib.sha256(str(v).encode()).hexdigest()[:16] if v is not None else None


# ── edges ──────────────────────────────────────────────────────────────────
def _apply_edges(conn, source: Source, parsed: Parsed, key_to_id: dict,
                 run_id: int, fetch_id: int, now: str) -> tuple[int, int]:
    opened = closed = 0
    for u in parsed.units:
        child = key_to_id.get(u.anchor_key)
        if child is None:
            continue
        parent = key_to_id.get(u.parent_anchor_key) if u.parent_anchor_key else None

        cur = conn.execute(
            "SELECT * FROM unit_edges WHERE child_id = ? AND source = ? "
            "AND edge_kind = ? AND valid_to IS NULL",
            (child, source.name, source.edge_kind),
        ).fetchone()

        if cur and cur["parent_id"] == parent:
            conn.execute("UPDATE unit_edges SET last_fetch_id = ? WHERE id = ?",
                         (fetch_id, cur["id"]))
            continue
        if cur:
            conn.execute(
                "UPDATE unit_edges SET valid_to = ?, valid_to_run = ? WHERE id = ?",
                (now, run_id, cur["id"]))
            closed += 1

        conn.execute(
            "INSERT INTO unit_edges (parent_id, child_id, edge_kind, source, method, "
            "valid_from, valid_from_run, open_fetch_id, last_fetch_id, source_url) "
            "VALUES (?,?,?,?,'published',?,?,?,?,?)",
            (parent, child, source.edge_kind, source.name, now, run_id,
             fetch_id, fetch_id, u.source_url),
        )
        opened += 1
    return opened, closed


# ── positions, persons, occupancies ────────────────────────────────────────
def _upsert_position(conn, source: Source, p, uid: int, now: str):
    row = conn.execute(
        "SELECT id FROM positions WHERE anchor_source = ? AND anchor_key = ?",
        (source.name, p.anchor_key)).fetchone()
    if row:
        return int(row["id"]), False
    cur = conn.execute(
        "INSERT INTO positions (unit_id, title, title_norm, appt_type, pay_plan, "
        "level_grade, location, is_pas, anchor_source, anchor_key, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (uid, p.title, normalize_name(p.title), p.appt_type, p.pay_plan,
         p.level_grade, p.location, int(p.is_pas), source.name, p.anchor_key, now),
    )
    return int(cur.lastrowid), True


def _upsert_occupancy(conn, source: Source, occ, pid: int, fetch_id: int, now: str) -> bool:
    display = " ".join(x for x in (occ.first_name, occ.last_name) if x).strip()
    if not display:
        return False
    norm = normalize_name(display)

    row = conn.execute("SELECT id FROM persons WHERE name_norm = ?", (norm,)).fetchone()
    if row:
        person_id = int(row["id"])
    else:
        cur = conn.execute(
            "INSERT INTO persons (display_name, name_norm, first_name, last_name, "
            "created_at) VALUES (?,?,?,?,?)",
            (display.title(), norm, occ.first_name, occ.last_name, now))
        person_id = int(cur.lastrowid)

    existing = conn.execute(
        "SELECT id, status, valid_to FROM occupancies WHERE position_id = ? "
        "AND person_id = ? AND (valid_from IS ? OR valid_from = ?)",
        (pid, person_id, occ.valid_from, occ.valid_from)).fetchone()
    if existing:
        conn.execute(
            "UPDATE occupancies SET status = ?, valid_to = ?, last_fetch_id = ? "
            "WHERE id = ?",
            (occ.status, occ.valid_to, fetch_id, existing["id"]))
        return False

    conn.execute(
        "INSERT INTO occupancies (position_id, person_id, status, is_acting, "
        "valid_from, valid_to, period_start, period_end, source, method, "
        "confidence, open_fetch_id, last_fetch_id, source_url) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, person_id, occ.status, int(occ.is_acting), occ.valid_from,
         occ.valid_to, occ.period_start, occ.period_end, source.name, occ.method,
         occ.confidence, fetch_id, fetch_id, occ.source_url),
    )
    return True


# ── change log ─────────────────────────────────────────────────────────────
def _change_row(conn, uid: int, s, outcome: str, run_id: int, fetch_id: int, now: str):
    mat = materiality(s.predicate)
    suppressed = outcome == "cosmetic"
    reason = ("normalized value unchanged; raw text differs "
              f"(predicate class {mat})") if suppressed else None
    prev = conn.execute(
        "SELECT value_raw FROM statements WHERE unit_id = ? AND predicate = ? "
        "AND valid_to IS NOT NULL ORDER BY valid_to_run DESC LIMIT 1",
        (uid, s.predicate)).fetchone()
    return (run_id, uid, "renamed" if s.predicate == "name" else "changed",
            mat, mat, int(suppressed), reason,
            prev["value_raw"] if prev else None, s.value,
            # effective_from is populated ONLY when a source states one; the
            # renderer must say "observed" whenever it is NULL (plan §11 R7).
            s.effective_from, now, fetch_id, s.source_url, s.predicate)


def _insert_change(conn, row) -> None:
    (run_id, uid, kind, mat, _m2, suppressed, reason, old, new,
     effective_from, now, fetch_id, url, predicate) = row
    conn.execute(
        "INSERT INTO changes (run_id, unit_id, kind, predicate, old_value, "
        "new_value, materiality, suppressed, suppress_reason, rules_version, "
        "observed_at, effective_from, fetch_id, source_url) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, uid, kind, predicate, old, new, mat, suppressed, reason,
         RULES_VERSION, now, effective_from, fetch_id, url),
    )
