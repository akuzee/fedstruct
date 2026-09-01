"""Preflight. Run #1 is unrepeatable, so this gates it (plan §11 R0).

The checks that matter are not "did it return 200" but the SPECIFIC known
failure signatures. A generic reachability check passes happily while
federalregister.gov serves an interstitial that parses to zero agencies. Those
assertions are what turn a silent future breakage into a red line.

Shape (green/red/yellow, exit 1 on hard fail) follows composter's doctor.py.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from .config import Config
from .http import FetchFailed, Session, SourceBlocked

OK, FAIL, WARN, INFO = "ok", "fail", "warn", "info"
_MARK = {OK: "✓", FAIL: "✗", WARN: "!", INFO: "•"}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""

    def line(self) -> str:
        return f"  {_MARK[self.status]} {self.name}" + (f" — {self.detail}" if self.detail else "")


def run_checks(cfg: Config, *, network: bool = True, session: Session | None = None) -> list[Check]:
    checks: list[Check] = []

    # ── environment ────────────────────────────────────────────────────────
    v = sys.version_info
    checks.append(Check(
        f"python {v.major}.{v.minor}.{v.micro}",
        OK if (v.major, v.minor) >= (3, 11) else FAIL,
        "" if (v.major, v.minor) >= (3, 11) else "3.11+ required",
    ))

    try:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.state_dir / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
        checks.append(Check(f"state dir writable ({cfg.state_dir})", OK))
    except OSError as exc:
        checks.append(Check("state dir writable", FAIL, str(exc)))

    # ── schema and run ledger ──────────────────────────────────────────────
    if cfg.db_path.exists():
        try:
            from .db import connect

            conn = connect(cfg.db_path, create=False)
            n = conn.execute(
                "SELECT COUNT(*) c FROM runs WHERE status = 'running'").fetchone()["c"]
            checks.append(Check("schema version matches", OK))
            checks.append(Check(
                "no orphaned runs", OK if not n else WARN,
                "" if not n else f"{n} run(s) stuck at 'running'; the next run reaps them"))
            conn.close()
        except Exception as exc:
            checks.append(Check("database opens", FAIL, str(exc)))
    else:
        checks.append(Check("database", INFO, f"not created yet ({cfg.db_path})"))

    # ── the hostility assertions (plan §10) ────────────────────────────────
    if network:
        sess = session or Session(user_agent=cfg.user_agent, contact=cfg.contact)
        checks.extend(_probe(sess, "federalregister.gov not blocking us",
                             "https://www.federalregister.gov/api/v1/agencies.json",
                             expect_json_array=True))
        checks.extend(_probe(sess, "ecfr.gov accepts our Accept-Encoding",
                             "https://www.ecfr.gov/api/admin/v1/agencies.json",
                             expect_json_key="agencies"))
        checks.extend(_probe(sess, "escs.opm.gov serving PLUM CSV",
                             "https://escs.opm.gov/escs-net/api/pbpub/download-data",
                             expect_csv_header="AgencyName"))
    else:
        checks.append(Check("network probes", INFO, "skipped (--offline)"))

    # ── secrets, only for what is actually requested ───────────────────────
    checks.append(Check(
        "ANTHROPIC_API_KEY", INFO if not cfg.anthropic_api_key else OK,
        "absent — fine; the scheduled path needs no secrets"
        if not cfg.anthropic_api_key else "set"))

    return checks


def _probe(sess: Session, name: str, url: str, *, expect_json_array: bool = False,
           expect_json_key: str | None = None,
           expect_csv_header: str | None = None) -> list[Check]:
    try:
        r = sess.get(url)
    except SourceBlocked as exc:
        return [Check(name, FAIL, f"BLOCKED: {exc}")]
    except FetchFailed as exc:
        return [Check(name, FAIL, str(exc))]

    if r.status != 200:
        return [Check(name, FAIL, f"HTTP {r.status}")]

    # Status 200 is not enough. Assert the payload is the shape we expect,
    # because every one of these blocks returns 200 with the wrong body.
    try:
        if expect_json_array:
            d = r.json()
            if not isinstance(d, list) or not d:
                return [Check(name, FAIL, "200 but not a non-empty JSON array")]
            return [Check(name, OK, f"{len(d)} agencies")]
        if expect_json_key:
            d = r.json()
            if expect_json_key not in d:
                return [Check(name, FAIL, f"200 but no {expect_json_key!r} key")]
            return [Check(name, OK, f"{len(d[expect_json_key])} top-level agencies")]
        if expect_csv_header:
            head = r.text[:200]
            if expect_csv_header not in head:
                return [Check(name, FAIL, f"200 but no {expect_csv_header!r} header")]
            return [Check(name, OK, f"{len(r.content):,} bytes")]
    except Exception as exc:
        return [Check(name, FAIL, f"200 but unparseable: {exc}")]

    return [Check(name, OK)]


def report(checks: list[Check]) -> tuple[str, int]:
    lines = [c.line() for c in checks]
    failed = sum(1 for c in checks if c.status == FAIL)
    warned = sum(1 for c in checks if c.status == WARN)
    lines.append("")
    if failed:
        lines.append(f"{failed} check(s) FAILED. Run #1 is unrepeatable — do not "
                     f"ingest until these are green.")
    elif warned:
        lines.append(f"All hard checks passed; {warned} warning(s).")
    else:
        lines.append("All checks passed.")
    return "\n".join(lines), (1 if failed else 0)
