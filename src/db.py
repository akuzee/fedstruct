"""The ledger — state/fedstruct.sqlite, the only durable state (plan §3).

Storage model: statements, not a graph. Every fact is a row carrying its own
provenance and its own timelines. Entity tables mint and register ids; they
hold no attributes. The org chart is a VIEW.

Three timelines, never conflated:

    valid_from / valid_to        world time — when the fact was TRUE.
                                 Closed on change (slowly-changing-dimension 2).
    effective_from               a date a source explicitly STATED. Usually NULL.
    observed_first / _last       when the fetcher saw it.

Conflating world time with observation time is the single most common way a
temporal store starts lying, so the columns are kept apart and the changelog
renderer says "observed" wherever effective_from is NULL.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
-- ── §3 run ledger ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY,
    mode              TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT NOT NULL DEFAULT 'running',
    git_sha           TEXT,
    sources_checked   INTEGER NOT NULL DEFAULT 0,
    sources_changed   INTEGER NOT NULL DEFAULT 0,
    statements_opened INTEGER NOT NULL DEFAULT 0,
    statements_closed INTEGER NOT NULL DEFAULT 0,
    changes_emitted   INTEGER NOT NULL DEFAULT 0,
    http_requests     INTEGER NOT NULL DEFAULT 0,
    llm_cost_usd      REAL NOT NULL DEFAULT 0.0,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- ── §6 sources and fetch ledger ────────────────────────────────────────────
-- `tier` records HOW we detect change, and the design goal is that nothing
-- sits in tier 2:
--   declared -- the source publishes a freshness marker; probe it, don't
--               download the payload unless the marker moved
--   hash     -- no marker exists; fetch and sha256-compare
--   uncond   -- re-parse every time. A bug, not a strategy.
CREATE TABLE IF NOT EXISTS sources (
    name              TEXT PRIMARY KEY,
    tier              TEXT NOT NULL CHECK (tier IN ('declared','hash','uncond')),
    url               TEXT NOT NULL,
    cadence_days      INTEGER NOT NULL,
    scope_claim       TEXT,      -- the source's own completeness claim, quoted.
                                 -- NULL => this source may report PRESENT but
                                 -- may NEVER be the basis for an ABSENT (§6).
    last_checked      TEXT,
    last_changed      TEXT,
    last_content_hash TEXT,
    freshness_marker  TEXT,
    unchanged_streak  INTEGER NOT NULL DEFAULT 0,
    fail_streak       INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    enabled           INTEGER NOT NULL DEFAULT 1
);

-- One row per fetch attempt. Outlives the payload: after state/raw/ is pruned
-- the url + sha256 + fetched_at still support a citation.
CREATE TABLE IF NOT EXISTS fetches (
    id               INTEGER PRIMARY KEY,
    run_id           INTEGER NOT NULL REFERENCES runs(id),
    source           TEXT NOT NULL REFERENCES sources(name),
    url              TEXT NOT NULL,
    fetched_at       TEXT NOT NULL,
    http_status      INTEGER,
    bytes            INTEGER,
    sha256           TEXT,
    freshness_marker TEXT,
    outcome          TEXT NOT NULL CHECK (outcome IN
                       ('fresh','unchanged','not_due','failed','quarantined')),
    row_count        INTEGER,
    raw_path         TEXT,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetches_source ON fetches(source, id DESC);

-- ── §3/§4 units and identity ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS units (
    id             INTEGER PRIMARY KEY,
    slug           TEXT NOT NULL UNIQUE,
    canonical_name TEXT NOT NULL,
    name_norm      TEXT NOT NULL,
    jurisdiction   TEXT NOT NULL DEFAULT 'us-federal',
    branch         TEXT,
    kind           TEXT,
    lifecycle      TEXT NOT NULL DEFAULT 'active',
    anchor_source  TEXT NOT NULL REFERENCES sources(name),
    anchor_key     TEXT NOT NULL,
    first_seen_run INTEGER NOT NULL REFERENCES runs(id),
    last_seen_run  INTEGER REFERENCES runs(id),
    absent_runs    INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    -- Merges are non-destructive POINTERS, never statement rewrites. Undoing a
    -- bad merge is deleting one line of config/adjudications.yaml and
    -- rerunning; `merged_by` records which rule or adjudication did it, so a
    -- rule-set merge can be cleared without touching a human's decision.
    merged_into    INTEGER REFERENCES units(id),
    merged_by      TEXT,
    merge_method   TEXT,
    merge_evidence TEXT,
    UNIQUE (anchor_source, anchor_key)
);
CREATE INDEX IF NOT EXISTS idx_units_lifecycle ON units(lifecycle);
CREATE INDEX IF NOT EXISTS idx_units_namenorm  ON units(name_norm);

-- The crosswalk. PRIMARY KEY (scheme, key) structurally forbids one external
-- identifier fanning out across two units — a false merge invents edges, and a
-- false split fragments one agency while looking clean (plan §2).
--
-- `method` is the audit column: "which of these links rest on a name guess?"
-- is WHERE method = 'inferred'.
CREATE TABLE IF NOT EXISTS unit_keys (
    scheme          TEXT NOT NULL,
    key             TEXT NOT NULL,
    unit_id         INTEGER NOT NULL REFERENCES units(id),
    method          TEXT NOT NULL CHECK (method IN
                      ('published','crosswalk','derived','adjudicated',
                       'inferred','asserted')),
    confidence      REAL NOT NULL,
    fetch_id        INTEGER REFERENCES fetches(id),
    match_score     REAL,
    match_runner_up TEXT,
    reviewed_by     TEXT,
    reviewed_at     TEXT,
    PRIMARY KEY (scheme, key)
);
CREATE INDEX IF NOT EXISTS idx_unitkeys_unit ON unit_keys(unit_id);

-- ── §4 identity adjudication ───────────────────────────────────────────────
-- Candidates are PROPOSED here and merged only by a deterministic identifier
-- or a human. Name similarity generates rows in this table and nothing else:
-- a false merge invents edges, and it is not recoverable by inspection because
-- the result looks cleaner than the truth (plan §2).
CREATE TABLE IF NOT EXISTS merge_candidates (
    id            INTEGER PRIMARY KEY,
    left_id       INTEGER NOT NULL REFERENCES units(id),
    right_id      INTEGER NOT NULL REFERENCES units(id),
    score         REAL NOT NULL,
    signals_json  TEXT NOT NULL,   -- every evidence axis, so a human sees WHY
    status        TEXT NOT NULL DEFAULT 'open',
                                   -- open|auto_bound|confirmed|rejected|deferred
    bind_method   TEXT,            -- how it was bound, if it was
    proposed_by   TEXT NOT NULL,   -- rule version that generated it
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    resolved_at   TEXT,
    -- Canonical ordering: prevents an A/B and a B/A row for the same pair
    -- without any application-level dedupe.
    CHECK (left_id < right_id),
    UNIQUE (left_id, right_id)
);
CREATE INDEX IF NOT EXISTS idx_cand_status ON merge_candidates(status, score DESC);

-- Materialized from config/adjudications.yaml on every `resolve`, rebuilt from
-- scratch, so deleting a YAML entry un-merges on the next run.
CREATE TABLE IF NOT EXISTS adjudications (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,    -- merge | never_merge | identifier | retract
    payload_json TEXT NOT NULL,
    evidence     TEXT NOT NULL,
    decided_by   TEXT NOT NULL,
    decided_on   TEXT NOT NULL,
    applied_at   TEXT,
    apply_error  TEXT
);

-- ── §3 statements ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS statements (
    id             INTEGER PRIMARY KEY,
    unit_id        INTEGER NOT NULL REFERENCES units(id),
    predicate      TEXT NOT NULL,
    -- Discriminator for MULTI-VALUED predicates. '' for single-valued facts
    -- (a unit has one name), and the value itself for repeating ones (a unit
    -- holds many CFR chapters). Without it, the second cfr_chapter row would
    -- close the first, and gaining a chapter would masquerade as losing one.
    value_key      TEXT NOT NULL DEFAULT '',
    value_norm     TEXT,      -- what versioning COMPARES
    value_raw      TEXT,      -- what the UI QUOTES
    value_num      REAL,
    value_unit     TEXT,
    source         TEXT NOT NULL REFERENCES sources(name),
    method         TEXT NOT NULL,
    confidence     REAL NOT NULL DEFAULT 1.0,
    via_key        TEXT,      -- set when the unit join itself was inferred;
                              -- weakest-link propagation reads this (§7)
    valid_from     TEXT NOT NULL,
    valid_from_run INTEGER NOT NULL REFERENCES runs(id),
    valid_to       TEXT,
    valid_to_run   INTEGER REFERENCES runs(id),
    valid_precision TEXT NOT NULL DEFAULT 'unknown',
    effective_from TEXT,      -- ONLY from a source that states one
    effective_src  TEXT,
    period_start   TEXT,      -- the OFFICE's term (same for every holder)
    period_end     TEXT,
    open_fetch_id  INTEGER NOT NULL REFERENCES fetches(id),
    last_fetch_id  INTEGER NOT NULL REFERENCES fetches(id),
    observations   INTEGER NOT NULL DEFAULT 1,
    cosmetic_revisions INTEGER NOT NULL DEFAULT 0,
    raw_hash       TEXT,
    source_url     TEXT NOT NULL,
    source_locator TEXT,
    qualifiers_json TEXT
);
-- At most one OPEN row per (unit, predicate, source, value_key). Every
-- temporal-store bug -- double-open intervals, lost closes, resurrected rows
-- -- becomes an IntegrityError at write time instead of a silently wrong page.
CREATE UNIQUE INDEX IF NOT EXISTS idx_stmt_current
    ON statements(unit_id, predicate, source, value_key) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_stmt_unit ON statements(unit_id, predicate);
CREATE INDEX IF NOT EXISTS idx_stmt_from ON statements(valid_from_run);
CREATE INDEX IF NOT EXISTS idx_stmt_to   ON statements(valid_to_run);

-- Hierarchy gets its own typed table rather than living as a predicate: a
-- WITH RECURSIVE over two typed columns is readable, over EAV it is not.
--
-- The unique index is per (child, source), NOT per child. Two sources
-- disagreeing about who a bureau reports to is a representable state and is
-- itself a finding -- not a bug to resolve away.
CREATE TABLE IF NOT EXISTS unit_edges (
    id             INTEGER PRIMARY KEY,
    parent_id      INTEGER REFERENCES units(id),   -- NULL = root
    child_id       INTEGER NOT NULL REFERENCES units(id),
    edge_kind      TEXT NOT NULL DEFAULT 'org',    -- org | financial | advisory
    source         TEXT NOT NULL REFERENCES sources(name),
    method         TEXT NOT NULL,
    confidence     REAL NOT NULL DEFAULT 1.0,
    valid_from     TEXT NOT NULL,
    valid_from_run INTEGER NOT NULL REFERENCES runs(id),
    valid_to       TEXT,
    valid_to_run   INTEGER REFERENCES runs(id),
    effective_from TEXT,
    open_fetch_id  INTEGER NOT NULL REFERENCES fetches(id),
    last_fetch_id  INTEGER NOT NULL REFERENCES fetches(id),
    source_url     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_current
    ON unit_edges(child_id, source, edge_kind) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_edge_parent ON unit_edges(parent_id);

-- ── positions and people (§3) ──────────────────────────────────────────────
-- Position and Person are separate from Occupancy so that a vacancy, an acting
-- official, and a reorganization are all representable. Modelling
-- person-reports-to-person cannot express any of the three, and federal
-- reality is saturated with all of them.
CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY,
    unit_id       INTEGER NOT NULL REFERENCES units(id),
    title         TEXT NOT NULL,
    title_norm    TEXT NOT NULL,
    appt_type     TEXT,        -- PAS, PA, SC, CA, ...
    pay_plan      TEXT,
    level_grade   TEXT,
    location      TEXT,
    is_pas        INTEGER NOT NULL DEFAULT 0,
    anchor_source TEXT NOT NULL REFERENCES sources(name),
    anchor_key    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (anchor_source, anchor_key)
);
CREATE INDEX IF NOT EXISTS idx_positions_unit ON positions(unit_id);

CREATE TABLE IF NOT EXISTS persons (
    id           INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    name_norm    TEXT NOT NULL,
    first_name   TEXT,
    last_name    TEXT,
    wikidata_qid TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (name_norm)
);

-- valid_from/valid_to = THIS person's tenure.
-- period_start/period_end = the OFFICE's term, identical for every holder.
-- An acting official has a tenure and NO term, which is exactly what makes the
-- Federal Vacancies Reform Act distinction representable.
CREATE TABLE IF NOT EXISTS occupancies (
    id             INTEGER PRIMARY KEY,
    position_id    INTEGER NOT NULL REFERENCES positions(id),
    person_id      INTEGER NOT NULL REFERENCES persons(id),
    status         TEXT NOT NULL,       -- current | ended
    is_acting      INTEGER NOT NULL DEFAULT 0,
    valid_from     TEXT,
    valid_to       TEXT,
    period_start   TEXT,
    period_end     TEXT,
    source         TEXT NOT NULL REFERENCES sources(name),
    method         TEXT NOT NULL,
    confidence     REAL NOT NULL DEFAULT 1.0,
    open_fetch_id  INTEGER NOT NULL REFERENCES fetches(id),
    last_fetch_id  INTEGER NOT NULL REFERENCES fetches(id),
    source_url     TEXT NOT NULL,
    UNIQUE (position_id, person_id, valid_from)
);
CREATE INDEX IF NOT EXISTS idx_occ_position ON occupancies(position_id);
CREATE INDEX IF NOT EXISTS idx_occ_person   ON occupancies(person_id);

-- ── §6 change log ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS changes (
    id               INTEGER PRIMARY KEY,
    run_id           INTEGER NOT NULL REFERENCES runs(id),
    unit_id          INTEGER REFERENCES units(id),
    kind             TEXT NOT NULL,   -- appeared | disappeared | parent_changed
                                      -- | renamed | quantitative | went_dark
    predicate        TEXT,
    old_value        TEXT,
    new_value        TEXT,
    materiality      TEXT NOT NULL,   -- structural|nominal|quantitative|cosmetic
    suppressed       INTEGER NOT NULL DEFAULT 0,
    suppress_reason  TEXT,
    corroboration    INTEGER NOT NULL DEFAULT 0,
    rules_version    TEXT NOT NULL,
    verdict          TEXT,
    verdict_source   TEXT,            -- rule | llm | human
    verdict_conf     REAL,
    verdict_locked   INTEGER NOT NULL DEFAULT 0,
    rationale        TEXT,
    observed_at      TEXT NOT NULL,
    effective_from   TEXT,            -- NULL => the renderer must say "observed"
    fetch_id         INTEGER REFERENCES fetches(id),
    source_url       TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_run  ON changes(run_id);
CREATE INDEX IF NOT EXISTS idx_changes_unit ON changes(unit_id, observed_at DESC);

-- ── §6 availability registry ───────────────────────────────────────────────
-- Rebuilt in place every run: it is a materialized view, and rebuilding it is
-- how "every phase idempotent" extends into this subsystem.
CREATE TABLE IF NOT EXISTS coverage (
    unit_id      INTEGER NOT NULL REFERENCES units(id),
    facet        TEXT NOT NULL,
    probe        TEXT NOT NULL,
    state        TEXT NOT NULL CHECK (state IN
                   ('PRESENT','ABSENT','NOT_APPLICABLE','NOT_CHECKED','BLOCKED')),
    reason       TEXT,       -- the NA rule name, surfaced in the UI
    value        TEXT,
    count        INTEGER,
    method       TEXT NOT NULL,
    confidence   REAL NOT NULL,
    fetch_id     INTEGER REFERENCES fetches(id),
    source_url   TEXT,
    stale_since  TEXT,       -- set when carried forward past a BLOCKED probe
    computed_at  TEXT NOT NULL,
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    PRIMARY KEY (unit_id, facet, probe)
);
CREATE INDEX IF NOT EXISTS idx_coverage_facet ON coverage(facet, state);

CREATE TABLE IF NOT EXISTS coverage_history (
    unit_id        INTEGER NOT NULL REFERENCES units(id),
    facet          TEXT NOT NULL,
    state          TEXT NOT NULL,
    valid_from     TEXT NOT NULL,
    valid_from_run INTEGER NOT NULL,
    valid_to       TEXT,
    valid_to_run   INTEGER,
    PRIMARY KEY (unit_id, facet, valid_from)
);

-- Curated, not derived -- an external audit finding is by nature not
-- machine-derivable. This is the one deliberate exception to "derived, not
-- curated", and the README says so (plan §7).
CREATE TABLE IF NOT EXISTS source_caveats (
    id                INTEGER PRIMARY KEY,
    source            TEXT NOT NULL REFERENCES sources(name),
    facet             TEXT,
    kind              TEXT NOT NULL,   -- incompleteness|staleness|scope_limit
    summary           TEXT NOT NULL,
    citation          TEXT NOT NULL,
    citation_url      TEXT NOT NULL,
    applies_from      TEXT,
    applies_to        TEXT,
    downgrades_absent INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES runs(id),
    task          TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cache_read    INTEGER,
    cost_usd      REAL NOT NULL,
    ok            INTEGER NOT NULL DEFAULT 1,
    called_at     TEXT NOT NULL
);

-- ── views: the graph is derived, never stored ──────────────────────────────
-- Resolve a unit through any chain of merge pointers to its survivor.
CREATE VIEW IF NOT EXISTS v_canonical AS
    WITH RECURSIVE r(id, canonical_id) AS (
        SELECT id, id FROM units WHERE merged_into IS NULL
        UNION ALL
        SELECT u.id, r.canonical_id FROM units u JOIN r ON u.merged_into = r.id
    ) SELECT * FROM r;

CREATE VIEW IF NOT EXISTS v_current_edges AS
    SELECT e.*, p.slug AS parent_slug, c.slug AS child_slug
      FROM unit_edges e
      LEFT JOIN units p ON p.id = e.parent_id
      JOIN units c ON c.id = e.child_id
     WHERE e.valid_to IS NULL;

CREATE VIEW IF NOT EXISTS v_current_statements AS
    SELECT * FROM statements WHERE valid_to IS NULL;

-- Two sources disagreeing is a feature, and this view is what the UI badges.
CREATE VIEW IF NOT EXISTS v_contested_parent AS
    SELECT child_id, COUNT(DISTINCT parent_id) AS n_parents,
           GROUP_CONCAT(DISTINCT source) AS sources
      FROM unit_edges
     WHERE valid_to IS NULL AND edge_kind = 'org'
     GROUP BY child_id
    HAVING n_parents > 1;
"""


class SchemaVersionMismatch(RuntimeError):
    """The DB on disk was written by a different schema version."""


def connect(db_path: Path, *, create: bool = True) -> sqlite3.Connection:
    """Open the ledger, applying SCHEMA and checking SCHEMA_VERSION.

    Foreign keys are OFF by default in SQLite and must be enabled per
    connection; WAL keeps a long ingest from blocking a concurrent read.
    """
    db_path = Path(db_path)
    if not create and not db_path.exists():
        raise FileNotFoundError(f"no database at {db_path} (run `init` first)")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # isolation_level=None disables the driver's implicit transaction handling,
    # so ingest.py can own its BEGIN/COMMIT/ROLLBACK explicitly. Without it the
    # driver has already opened a transaction and an explicit BEGIN raises.
    # Every other write path is a single statement and autocommits.
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)

    found = get_kv(conn, "schema_version")
    if found is None:
        set_kv(conn, "schema_version", str(SCHEMA_VERSION))
    elif int(found) != SCHEMA_VERSION:
        raise SchemaVersionMismatch(
            f"database at {db_path} is schema v{found}, code expects "
            f"v{SCHEMA_VERSION}. Rebuild with `parse --reparse` from state/raw/ "
            f"(plan §11 R21) rather than migrating in place."
        )
    conn.commit()
    return conn


def get_kv(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_kv(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ── run ledger ─────────────────────────────────────────────────────────────
# A runner can vanish mid-run (GitHub Actions), leaving a row stuck at
# 'running' forever. Each source's ingest is a single transaction and output/
# is only rebuilt after every source commits, so an orphaned run leaves the DB
# consistent and the next run simply redoes the work.
ORPHAN_AFTER_HOURS = 6


def start_run(conn: sqlite3.Connection, mode: str, now: str, git_sha: str | None = None) -> int:
    reap_orphans(conn, now)
    cur = conn.execute(
        "INSERT INTO runs (mode, started_at, status, git_sha) VALUES (?, ?, 'running', ?)",
        (mode, now, git_sha),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, now: str, status: str,
               notes: str | None = None, **counters: int | float) -> None:
    cols = ", ".join(f"{k} = ?" for k in counters)
    sql = "UPDATE runs SET finished_at = ?, status = ?, notes = ?"
    params: list = [now, status, notes]
    if cols:
        sql += ", " + cols
        params.extend(counters.values())
    sql += " WHERE id = ?"
    params.append(run_id)
    conn.execute(sql, params)
    conn.commit()


def reap_orphans(conn: sqlite3.Connection, now: str) -> int:
    from datetime import datetime, timedelta

    cutoff = (datetime.fromisoformat(now) - timedelta(hours=ORPHAN_AFTER_HOURS)).isoformat()
    cur = conn.execute(
        "UPDATE runs SET status = 'orphaned', "
        "notes = 'no finished_at; runner vanished' "
        "WHERE status = 'running' AND started_at < ?",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount
