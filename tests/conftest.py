"""Test harness: a throwaway project under tmp_path, and no network, ever.

`http.Session` is injected rather than constructed at point of use, so tests
pass a FakeSession serving byte-frozen fixtures captured from real responses.
A test that reaches the internet is a test that fails in CI at 3am for reasons
unrelated to the code.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import db, fetch as fetchmod, sources  # noqa: E402
from src.config import parse_config  # noqa: E402
from src.http import Response, SourceBlocked  # noqa: E402
from src.ingest import ingest  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


class FakeSession:
    """Serves fixture bytes. Counts requests, so the sha256 gate is testable."""

    def __init__(self, bodies: dict[str, bytes]):
        self.bodies = bodies
        self.request_count = 0
        self.raise_for: dict[str, Exception] = {}

    def get(self, url: str, **kw) -> Response:
        self.request_count += 1
        for frag, exc in self.raise_for.items():
            if frag in url:
                raise exc
        for frag, body in self.bodies.items():
            if frag in url:
                return Response(url=url, status=200, content=body, headers={})
        raise AssertionError(f"FakeSession has no fixture for {url}")

    def set_body(self, frag: str, body: bytes) -> None:
        for k in list(self.bodies):
            if frag in k:
                self.bodies[k] = body
                return
        self.bodies[frag] = body


@dataclass
class Env:
    """One disposable project: config, database, fake network."""
    root: Path
    cfg: object
    conn: object
    sess: FakeSession

    def run_all(self, *, force: bool = False, now: str = "2026-09-01T00:00:00+00:00"):
        """fetch -> parse for every enabled source. Returns {source: IngestResult}."""
        run_id = db.start_run(self.conn, "test", now)
        out = {}
        for name in self.cfg.enabled_sources:
            src = sources.get(name)
            r = fetchmod.check_source(self.conn, self.cfg, self.sess, name, run_id,
                                      now, force=force, suffix=src.raw_suffix)
            if not r.changed:
                out[name] = r
                continue
            parsed = src.parse(r.body)
            reason = fetchmod.sanity_check_rowcount(
                self.conn, name, parsed.row_count, self.cfg.rowcount_sanity_pct)
            if reason and not force:
                fetchmod.quarantine(self.conn, r.fetch_id, reason)
                out[name] = reason
                continue
            fetchmod.set_row_count(self.conn, r.fetch_id, parsed.row_count)
            out[name] = ingest(self.conn, src, parsed, run_id=run_id,
                               fetch_id=r.fetch_id, now=now,
                               max_changes=self.cfg.max_changes_per_run)
        db.finish_run(self.conn, run_id, now, "ok")
        return out

    def q(self, sql: str, *args):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, *args):
        row = self.conn.execute(sql, args).fetchone()
        return row[0] if row else None


@pytest.fixture
def bodies() -> dict[str, bytes]:
    return {
        "federalregister.gov": (FIXTURES / "fr_agencies.json").read_bytes(),
        "ecfr.gov": (FIXTURES / "ecfr_agencies.json").read_bytes(),
        "escs.opm.gov": (FIXTURES / "plum.csv").read_bytes(),
    }


@pytest.fixture
def env(tmp_path, bodies) -> Env:
    cfg = parse_config({
        "state_dir": "state",
        "output_dir": "output",
        "enabled_sources": ["fr_agencies", "ecfr_agencies", "plum"],
        "max_changes_per_run": 40,
    }, tmp_path)
    conn = db.connect(cfg.db_path)
    for name in cfg.enabled_sources:
        src = sources.get(name)
        conn.execute(
            "INSERT INTO sources (name, tier, url, cadence_days, scope_claim) "
            "VALUES (?,?,?,?,?)",
            (src.name, src.tier, src.url, cfg.cadence(src.name), src.scope_claim))
    conn.commit()
    return Env(root=tmp_path, cfg=cfg, conn=conn, sess=FakeSession(bodies))


@pytest.fixture
def ingested(env) -> Env:
    env.run_all()
    return env
