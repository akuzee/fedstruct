"""CLI entry point: python -m src.main <verb>.

Every cmd_* returns a dict and main prints json.dumps(..., indent=2), so every
verb is pipeable into jq and every run is inspectable without a database
client.

`all` composes fetch -> parse -> build and deliberately EXCLUDES any verb that
spends money. The default composable run is free and offline-safe; spending is
always an explicit choice.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db, fetch as fetchmod, sources
from .config import PROJECT_ROOT, Config, load_config, load_dotenv
from .ingest import ingest


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def git_sha() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


# ── init ───────────────────────────────────────────────────────────────────
def cmd_init(cfg: Config, args) -> dict:
    conn = db.connect(cfg.db_path)
    run_id = db.start_run(conn, "init", now_iso(), git_sha())
    seeded = []
    for name in cfg.enabled_sources:
        src = sources.get(name)
        conn.execute(
            "INSERT INTO sources (name, tier, url, cadence_days, scope_claim, enabled) "
            "VALUES (?,?,?,?,?,1) ON CONFLICT(name) DO UPDATE SET "
            "url = excluded.url, tier = excluded.tier, "
            "cadence_days = excluded.cadence_days, scope_claim = excluded.scope_claim",
            (src.name, src.tier, src.url, cfg.cadence(src.name), src.scope_claim),
        )
        seeded.append(src.name)

    n_caveats = _seed_caveats(conn, cfg)
    conn.commit()
    db.finish_run(conn, run_id, now_iso(), "ok")
    return {"db": str(cfg.db_path), "schema_version": db.SCHEMA_VERSION,
            "sources": seeded, "caveats": n_caveats}


def _seed_caveats(conn, cfg: Config) -> int:
    """Load config/caveats.yaml — the one deliberately hand-curated input.

    An external audit finding is by nature not machine-derivable, which is why
    this exception exists and why the README names it (plan §7).
    """
    import yaml

    path = cfg.project_root / "config" / "caveats.yaml"
    if not path.exists():
        return 0
    entries = yaml.safe_load(path.read_text()) or []
    conn.execute("DELETE FROM source_caveats")
    for e in entries:
        conn.execute(
            "INSERT INTO source_caveats (source, facet, kind, summary, citation, "
            "citation_url, applies_from, applies_to, downgrades_absent) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (e["source"], e.get("facet"), e["kind"], e["summary"].strip(),
             e["citation"], e["citation_url"], e.get("applies_from"),
             e.get("applies_to"), int(e.get("downgrades_absent", True))),
        )
    return len(entries)


# ── doctor ─────────────────────────────────────────────────────────────────
def cmd_doctor(cfg: Config, args) -> dict:
    from .doctor import report, run_checks

    checks = run_checks(cfg, network=not args.offline)
    text, code = report(checks)
    print(text, file=sys.stderr)
    return {"checks": [{"name": c.name, "status": c.status, "detail": c.detail}
                       for c in checks],
            "exit_code": code}


# ── fetch + parse ──────────────────────────────────────────────────────────
def cmd_fetch(cfg: Config, args) -> dict:
    from .http import Session

    conn = db.connect(cfg.db_path, create=False)
    run_id = db.start_run(conn, "fetch", now_iso(), git_sha())
    sess = Session(user_agent=cfg.user_agent, contact=cfg.contact)
    names = [args.source] if args.source else list(cfg.enabled_sources)

    results = {}
    for name in names:
        src = sources.get(name)
        r = fetchmod.check_source(conn, cfg, sess, name, run_id, now_iso(),
                                  force=args.force, suffix=src.raw_suffix)
        results[name] = {"outcome": r.outcome, "fetch_id": r.fetch_id,
                         "sha256": (r.sha256 or "")[:12] or None, "error": r.error}

    changed = sum(1 for v in results.values() if v["outcome"] == "fresh")
    failed = any(v["outcome"] == "failed" for v in results.values())
    db.finish_run(conn, run_id, now_iso(), "failed" if failed else "ok",
                  sources_checked=len(names), sources_changed=changed,
                  http_requests=sess.request_count)
    return {"run_id": run_id, "checked": len(names), "changed": changed,
            "http_requests": sess.request_count, "sources": results}


def cmd_parse(cfg: Config, args) -> dict:
    conn = db.connect(cfg.db_path, create=False)
    run_id = db.start_run(conn, "parse", now_iso(), git_sha())
    names = [args.source] if args.source else list(cfg.enabled_sources)
    out, totals = {}, {"opened": 0, "closed": 0, "changes": 0, "suppressed": 0}

    for name in names:
        src = sources.get(name)
        row = conn.execute(
            "SELECT * FROM fetches WHERE source = ? AND outcome = 'fresh' "
            "AND raw_path IS NOT NULL ORDER BY id DESC LIMIT 1", (name,)).fetchone()
        if row is None:
            out[name] = {"skipped": "no fresh payload — run `fetch` first"}
            continue

        body = fetchmod.read_raw(cfg, row["raw_path"])
        parsed = src.parse(body)

        # A truncated response and a mass abolition look identical to a differ,
        # and only one of them is real.
        reason = fetchmod.sanity_check_rowcount(
            conn, name, parsed.row_count, cfg.rowcount_sanity_pct)
        if reason and not args.force:
            fetchmod.quarantine(conn, row["id"], reason)
            out[name] = {"quarantined": reason}
            continue
        fetchmod.set_row_count(conn, row["id"], parsed.row_count)

        res = ingest(conn, src, parsed, run_id=run_id, fetch_id=row["id"],
                     now=now_iso(), max_changes=cfg.max_changes_per_run)
        out[name] = res.as_dict()
        if not res.quarantined:
            totals["opened"] += res.statements_opened
            totals["closed"] += res.statements_closed
            totals["changes"] += res.changes
            totals["suppressed"] += res.suppressed

    conn.commit()
    db.finish_run(conn, run_id, now_iso(), "ok",
                  statements_opened=totals["opened"],
                  statements_closed=totals["closed"],
                  changes_emitted=totals["changes"])
    return {"run_id": run_id, **totals, "sources": out}


# ── status ─────────────────────────────────────────────────────────────────
def cmd_status(cfg: Config, args) -> dict:
    from datetime import date

    conn = db.connect(cfg.db_path, create=False)
    today = date.today()

    srcs = []
    for s in conn.execute("SELECT * FROM sources ORDER BY name"):
        srcs.append({
            "source": s["name"],
            "tier": s["tier"],
            # Computed at read time, never stored: a human opening the repo
            # must see "last poll: 94 days ago" even though nothing ran to
            # write it (plan §6).
            "days_since_check": _days(s["last_checked"], today),
            "days_since_change": _days(s["last_changed"], today),
            "due": bool(fetchmod.is_due(s, today)),
            "next_interval_days": fetchmod.interval_for(s, s["cadence_days"]),
            "unchanged_streak": s["unchanged_streak"],
            "fail_streak": s["fail_streak"],
            "last_error": s["last_error"],
        })

    counts = {
        k: conn.execute(q).fetchone()["c"] for k, q in {
            "units": "SELECT COUNT(*) c FROM units",
            "open_statements": "SELECT COUNT(*) c FROM statements WHERE valid_to IS NULL",
            "closed_statements": "SELECT COUNT(*) c FROM statements WHERE valid_to IS NOT NULL",
            "org_edges": "SELECT COUNT(*) c FROM v_current_edges WHERE edge_kind='org'",
            "positions": "SELECT COUNT(*) c FROM positions",
            "persons": "SELECT COUNT(*) c FROM persons",
            "current_occupancies": "SELECT COUNT(*) c FROM occupancies WHERE status='current'",
            "contested_parents": "SELECT COUNT(*) c FROM v_contested_parent",
        }.items()
    }

    # The headline metric (plan §9). It FALLS when a bad merge is split, which
    # is honest in the direction that matters.
    total = counts["units"] or 1
    multi = conn.execute(
        "SELECT COUNT(*) c FROM (SELECT unit_id FROM unit_keys GROUP BY unit_id "
        "HAVING COUNT(DISTINCT scheme) >= 3)").fetchone()["c"]

    return {
        "sources": srcs,
        "counts": counts,
        "reconciliation": {
            "units_with_3plus_source_bindings": multi,
            "pct": round(100 * multi / total, 1),
            "note": "headline metric; falls when a bad merge is split",
        },
    }


def _days(ts: str | None, today) -> int | None:
    if not ts:
        return None
    return (today - datetime.fromisoformat(ts).date()).days


# ── resolve / adjudicate ───────────────────────────────────────────────────
def cmd_resolve(cfg: Config, args) -> dict:
    from . import resolve

    conn = db.connect(cfg.db_path, create=False)
    run_id = db.start_run(conn, "resolve", now_iso(), git_sha())
    out: dict = {"run_id": run_id}
    path = cfg.project_root / "config" / "adjudications.yaml"

    # Order matters: never_merge entries must be loaded before proposal so
    # they can block a pair from ever being scored.
    adj = resolve.apply_adjudications(conn, path, now_iso())
    out["adjudications"] = adj.as_dict()
    if adj.errors:
        out["adjudication_errors"] = adj.errors

    out["propose"] = resolve.propose(conn, now_iso()).as_dict()
    if args.apply:
        out["auto_bind"] = resolve.apply_auto(conn, now_iso()).as_dict()

    out["summary"] = {
        "units_total": conn.execute("SELECT COUNT(*) c FROM units").fetchone()["c"],
        "canonical_units": conn.execute(
            "SELECT COUNT(*) c FROM units WHERE merged_into IS NULL").fetchone()["c"],
        "open_candidates": conn.execute(
            "SELECT COUNT(*) c FROM merge_candidates WHERE status='open'").fetchone()["c"],
    }
    db.finish_run(conn, run_id, now_iso(), "ok")
    return out


def cmd_adjudicate(cfg: Config, args) -> dict:
    from . import resolve

    conn = db.connect(cfg.db_path, create=False)
    if args.export_yaml is not None:
        # stdout only — the tool drafts, the human commits.
        print(resolve.export_yaml(conn, args.export_yaml), file=sys.stderr)
        return {"exported": args.export_yaml}
    return {"open_candidates": resolve.list_open(conn, args.limit)}


def cmd_build(cfg: Config, args) -> dict:
    from . import views

    conn = db.connect(cfg.db_path, create=False)
    run_id = db.start_run(conn, "build", now_iso(), git_sha())
    result = views.build(conn, cfg.output_dir)
    db.finish_run(conn, run_id, now_iso(), "ok")
    return {"run_id": run_id, **result}


def cmd_commit_message(cfg: Config, args) -> str:
    """The git subject line IS the changelog headline.

    `git log --oneline -- state/` then reads as a refresh history for free, and
    it makes a quiet month obviously quiet.
    """
    conn = db.connect(cfg.db_path, create=False)
    run = conn.execute(
        "SELECT id, mode FROM runs WHERE status='ok' ORDER BY id DESC LIMIT 1").fetchone()
    if run is None:
        return "refresh: no completed run"

    counts = dict(conn.execute(
        "SELECT COALESCE(SUM(suppressed=0), 0) AS shown, "
        "COALESCE(SUM(suppressed=1), 0) AS hidden FROM changes "
        "WHERE run_id >= ?", (run["id"] - 4,)).fetchone())
    struct = conn.execute(
        "SELECT COUNT(*) c FROM changes WHERE materiality='structural' "
        "AND suppressed=0 AND run_id >= ?", (run["id"] - 4,)).fetchone()["c"]
    srcs = conn.execute(
        "SELECT COUNT(*) t, COALESCE(SUM(last_changed = last_checked), 0) ch "
        "FROM sources").fetchone()

    parts = [f"{struct} structural"]
    if counts["shown"] - struct:
        parts.append(f"{counts['shown'] - struct} other")
    if counts["hidden"]:
        parts.append(f"{counts['hidden']} cosmetic suppressed")
    return (f"refresh({run['mode']}): {', '.join(parts)}; "
            f"{srcs['t']} sources, {srcs['ch']} changed")


def cmd_all(cfg: Config, args) -> dict:
    args.apply = True
    return {"fetch": cmd_fetch(cfg, args), "parse": cmd_parse(cfg, args),
            "resolve": cmd_resolve(cfg, args), "build": cmd_build(cfg, args)}


# ── argparse ───────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fedstruct",
                                description="Federal government structure explorer")
    p.add_argument("--config", type=Path, default=None)
    sub = p.add_subparsers(dest="verb", required=True)

    sub.add_parser("init", help="create the database and seed sources")

    d = sub.add_parser("doctor", help="preflight; gates the unrepeatable run #1")
    d.add_argument("--offline", action="store_true", help="skip network probes")

    for name, help_ in (("fetch", "poll due sources through the sha256 gate"),
                        ("parse", "turn fetched payloads into statements"),
                        ("all", "fetch then parse (never spends money)")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--source", help="limit to one source")
        s.add_argument("--force", action="store_true",
                       help="ignore due-ness, the hash gate, and sanity gates")

    r = sub.add_parser("resolve", help="propose and apply cross-source identity links")
    r.add_argument("--apply", action="store_true",
                   help="bind candidates carrying a deterministic identifier")

    a = sub.add_parser("adjudicate", help="the human loop; prints to stdout only")
    a.add_argument("--limit", type=int, default=20)
    a.add_argument("--export-yaml", type=int, metavar="CANDIDATE_ID",
                   help="print a ready-to-paste adjudications.yaml block")

    sub.add_parser("commit-message",
                   help="print the changelog headline for the git subject")
    sub.add_parser("build", help="statements -> output/*.json (deterministic)")
    sub.add_parser("status", help="coverage, staleness, reconciliation metric")
    return p


VERBS = {"init": cmd_init, "doctor": cmd_doctor, "fetch": cmd_fetch,
         "parse": cmd_parse, "resolve": cmd_resolve, "adjudicate": cmd_adjudicate,
         "build": cmd_build, "all": cmd_all, "status": cmd_status,
         "commit-message": cmd_commit_message}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env")
    cfg = load_config(args.config)
    result = VERBS[args.verb](cfg, args)
    # commit-message returns a bare line for `git commit -m "$(...)"`.
    if isinstance(result, str):
        print(result)
        return 0
    print(json.dumps(result, indent=2, default=str))
    return int(result.get("exit_code", 0)) if isinstance(result, dict) else 0


if __name__ == "__main__":
    sys.exit(main())
