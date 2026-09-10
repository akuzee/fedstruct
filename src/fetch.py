"""Due-ness, the sha256 gate, and the content-addressed payload store (plan §6).

The gate is the whole cost model: an unchanged source costs exactly one HTTP
request, writes no body, parses nothing, and calls no LLM. Steady-state cost
after the first full run is therefore $0.

Tier 0 sources (`declared`) do better still — they publish a freshness marker,
so we probe a few KB and only download the payload when the marker moves.
"""

from __future__ import annotations

import gzip
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import (
    BACKOFF_INTERVAL_DAYS,
    BACKOFF_MAX_DAYS,
    FAIL_BACKOFF_DAYS,
    NEVER_BACKOFF,
    UNCHANGED_STREAK_THRESHOLD,
    Config,
)
from .http import FetchFailed, Session, SourceBlocked


@dataclass
class FetchResult:
    source: str
    outcome: str          # fresh | unchanged | not_due | failed | quarantined
    fetch_id: int | None
    sha256: str | None = None
    body: bytes | None = None
    raw_path: str | None = None
    row_count: int | None = None
    error: str | None = None

    @property
    def changed(self) -> bool:
        return self.outcome == "fresh"


def is_due(src, today: date) -> bool:
    """Mirrors venue-radar's poll.py:_is_due. Shape preserved deliberately."""
    if not src["enabled"]:
        return False
    if not src["last_checked"]:
        return True
    last = datetime.fromisoformat(src["last_checked"]).date()

    if src["fail_streak"]:
        idx = min(src["fail_streak"] - 1, len(FAIL_BACKOFF_DAYS) - 1)
        interval = FAIL_BACKOFF_DAYS[idx]
    elif (src["name"] not in NEVER_BACKOFF
          and src["unchanged_streak"] >= UNCHANGED_STREAK_THRESHOLD):
        interval = min(BACKOFF_INTERVAL_DAYS, BACKOFF_MAX_DAYS)
    else:
        interval = src["cadence_days"]

    return last <= today - timedelta(days=interval)


def interval_for(src, cadence_days: int) -> int:
    """The interval that WOULD apply next, for reporting in `status`."""
    if src["fail_streak"]:
        return FAIL_BACKOFF_DAYS[min(src["fail_streak"] - 1, len(FAIL_BACKOFF_DAYS) - 1)]
    if (src["name"] not in NEVER_BACKOFF
            and src["unchanged_streak"] >= UNCHANGED_STREAK_THRESHOLD):
        return min(BACKOFF_INTERVAL_DAYS, BACKOFF_MAX_DAYS)
    return cadence_days


def store_raw(cfg: Config, source: str, sha: str, body: bytes, suffix: str) -> str:
    """Content-addressed, gzipped, sharded by hash prefix.

    Identity IS the content, so re-fetching identical bytes writes nothing new.
    Returns a path relative to project_root so the DB stays portable across
    checkouts (this file is committed and cloned).
    """
    d = cfg.raw_dir / source / sha[:2]
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sha}{suffix}.gz"
    if not p.exists():
        p.write_bytes(gzip.compress(body))
    return str(p.relative_to(cfg.project_root))


def read_raw(cfg: Config, raw_path: str) -> bytes:
    return gzip.decompress((cfg.project_root / raw_path).read_bytes())


def check_source(conn, cfg: Config, sess: Session, source_name: str, run_id: int,
                 now: str, *, force: bool = False, suffix: str = ".json",
                 sniff=None) -> FetchResult:
    """Poll one source through the gate. The single place fetch state changes."""
    src = conn.execute("SELECT * FROM sources WHERE name = ?", (source_name,)).fetchone()
    if src is None:
        raise KeyError(f"unknown source {source_name!r} — run `init` first")

    today = date.fromisoformat(now[:10])
    if not force and not is_due(src, today):
        fid = _record(conn, run_id, src, now, outcome="not_due")
        return FetchResult(source_name, "not_due", fid)

    try:
        resp = sess.get(src["url"])
        # Shape check BEFORE the hash gate: a CDN block page can arrive as
        # HTTP 200 (verified: Akamai on escs.opm.gov from GitHub runners), and
        # if it were stored it would hash stably — the next identical block
        # page would read as 'unchanged' and the outage would go invisible.
        if sniff is not None:
            err = sniff(resp.content)
            if err:
                raise SourceBlocked(f"{src['url']} returned HTTP {resp.status} "
                                    f"but not this source's data: {err}")
    except (SourceBlocked, FetchFailed) as exc:
        # A block and a fetch failure are both `failed`. Neither may ever
        # become `unchanged`, and neither may yield an empty parse.
        conn.execute(
            "UPDATE sources SET last_checked = ?, fail_streak = fail_streak + 1, "
            "last_error = ? WHERE name = ?",
            (now, str(exc), source_name),
        )
        fid = _record(conn, run_id, src, now, outcome="failed", error=str(exc))
        conn.commit()
        return FetchResult(source_name, "failed", fid, error=str(exc))

    sha = hashlib.sha256(resp.content).hexdigest()

    if not force and sha == src["last_content_hash"]:
        streak = src["unchanged_streak"] + 1
        conn.execute(
            "UPDATE sources SET last_checked = ?, unchanged_streak = ?, "
            "fail_streak = 0, last_error = NULL WHERE name = ?",
            (now, streak, source_name),
        )
        fid = _record(conn, run_id, src, now, outcome="unchanged",
                      status=resp.status, size=len(resp.content), sha=sha)
        conn.commit()
        # One HTTP request. No body written, no parse, no LLM.
        return FetchResult(source_name, "unchanged", fid, sha256=sha)

    raw_path = store_raw(cfg, source_name, sha, resp.content, suffix)
    conn.execute(
        "UPDATE sources SET last_checked = ?, last_changed = ?, "
        "last_content_hash = ?, unchanged_streak = 0, fail_streak = 0, "
        "last_error = NULL WHERE name = ?",
        (now, now, sha, source_name),
    )
    fid = _record(conn, run_id, src, now, outcome="fresh", status=resp.status,
                  size=len(resp.content), sha=sha, raw_path=raw_path)
    conn.commit()
    return FetchResult(source_name, "fresh", fid, sha256=sha,
                       body=resp.content, raw_path=raw_path)


def quarantine(conn, fetch_id: int, reason: str) -> None:
    """Mark a fetch quarantined: ingested nowhere, zero changes emitted.

    A truncated response and a mass abolition look identical to a differ, and
    only one of them is real (plan §11 R1).
    """
    conn.execute("UPDATE fetches SET outcome = 'quarantined', error = ? WHERE id = ?",
                 (reason, fetch_id))
    conn.commit()


def sanity_check_rowcount(conn, source: str, new_count: int, pct: float) -> str | None:
    """Return a reason string if this fetch's row count is implausible."""
    row = conn.execute(
        "SELECT row_count FROM fetches WHERE source = ? AND outcome = 'fresh' "
        "AND row_count IS NOT NULL ORDER BY id DESC LIMIT 1",
        (source,),
    ).fetchone()
    if row is None or not row["row_count"]:
        return None
    prev = row["row_count"]
    if abs(new_count - prev) / prev > pct:
        return (f"row count {new_count} differs from previous successful fetch "
                f"({prev}) by more than {pct:.0%}")
    return None


def _record(conn, run_id, src, now, *, outcome, status=None, size=None, sha=None,
            raw_path=None, error=None, row_count=None) -> int:
    cur = conn.execute(
        "INSERT INTO fetches (run_id, source, url, fetched_at, http_status, bytes, "
        "sha256, outcome, row_count, raw_path, error) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, src["name"], src["url"], now, status, size, sha, outcome,
         row_count, raw_path, error),
    )
    return int(cur.lastrowid)


def set_row_count(conn, fetch_id: int, n: int) -> None:
    conn.execute("UPDATE fetches SET row_count = ? WHERE id = ?", (n, fetch_id))
