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


def run_checks(cfg: Config, *, network: bool = True, session: Session | None = None,
               warn_sources: bool = False) -> list[Check]:
    """`warn_sources` downgrades source-probe failures to warnings.

    For CI: one blocked source must not abort refreshing the others — the
    fetch layer records per-source failures properly (fail_streak, backoff,
    visible in `status`). The hard gate remains the default for humans,
    because run #1 is unrepeatable.
    """
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
    # Each source validates its OWN payload via Source.sniff, and doctor calls
    # that rather than keeping a second copy of the expected shape. The second
    # copy is how doctor came to warn about a 'missing AgencyName column' for a
    # week after the fetch path had already been taught OPM's new header.
    if network:
        from . import sources as source_registry

        sess = session or Session(user_agent=cfg.user_agent, contact=cfg.contact)
        probes: list[Check] = []
        for name in cfg.enabled_sources:
            src = source_registry.get(name)
            probes.extend(_probe(sess, f"{name} serving its data", src.url, src))
        if warn_sources:
            for c in probes:
                if c.status == FAIL:
                    c.status = WARN
        checks.extend(probes)
    else:
        checks.append(Check("network probes", INFO, "skipped (--offline)"))

    # ── secrets, only for what is actually requested ───────────────────────
    checks.append(Check(
        "ANTHROPIC_API_KEY", INFO if not cfg.anthropic_api_key else OK,
        "absent — fine; the scheduled path needs no secrets"
        if not cfg.anthropic_api_key else "set"))

    return checks


def _probe(sess: Session, name: str, url: str, src) -> list[Check]:
    try:
        r = sess.get(url)
    except SourceBlocked as exc:
        return [Check(name, FAIL, f"BLOCKED: {exc}")]
    except FetchFailed as exc:
        return [Check(name, FAIL, str(exc))]

    if r.status != 200:
        return [Check(name, FAIL, f"HTTP {r.status}")]

    # HTTP 200 is not enough: every documented failure here returns 200 with
    # the wrong body. Ask the source itself whether this is its data.
    err = src.sniff(r.content)
    if err:
        return [Check(name, FAIL, f"200 but {err}")]

    # Parsing is the strongest check available, and it is free.
    try:
        parsed = src.parse(r.content)
    except Exception as exc:
        return [Check(name, FAIL, f"200, right shape, but unparseable: {exc}")]

    detail = f"{len(parsed.units)} units"
    if parsed.positions:
        detail += f", {len(parsed.positions)} positions"
    return [Check(name, OK, detail)]


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
