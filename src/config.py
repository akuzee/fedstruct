"""Load and validate config/fedstruct.yaml into a frozen Config.

Everything downstream takes a Config instance; nothing else reads the YAML
file, and nothing reads environment variables. The load/parse split exists so
tests can build a Config from a dict against tmp_path without touching
config/ (the idiom is composter's, src/config.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "fedstruct.yaml"


class ConfigError(RuntimeError):
    pass


# ── cadence (plan §6) ──────────────────────────────────────────────────────
#
# These are CEILINGS ON STALENESS, not schedules. The stated requirement is
# "monthly positions, yearly structure", but that is a claim about the PRODUCT
# and about the EXPENSIVE operations, not about fetch frequency. Fetching
# agencies.json costs 500 KB and 300 ms; parsing costs nothing once the hash
# gate says "same". So we look more often than we expect change and let the
# gate absorb it. What "yearly" actually buys is permission for the costly work
# -- the 75 MiB OPM parquet, the USLM title zips, the LLM adjudication pass --
# to run rarely.
DEFAULT_CADENCE_DAYS = {
    "ecfr_titles": 7,     # ~10 KB, and it is the freshness oracle for ALL of
                          # eCFR. Weekly costs nothing and makes eCFR free.
    "nominations": 1,     # daily incremental by updateDate; also the heartbeat
    "fr_agencies": 30,    # no declared freshness anywhere; hash-gated
    "ecfr_agencies": 30,
    "plum": 30,           # statutory floor is annual; observed cadence is much
                          # faster (a June 2026 snapshot carried 275 starts
                          # dated 2026)
    "usaspending": 30,    # DATA Act submissions are monthly
    "opm_employment": 30,  # data is quarterly but the LISTING is 3 KB
    "uscode": 90,
    "govman": 120,        # annual, ~3-month lag, and it skipped 2023 entirely
}

# Backoff, carried over from venue-radar (14 -> 30 there; 30 -> 90 here, same
# shape scaled to a corpus that moves in months rather than weeks). The streak
# counts CONSECUTIVE UNCHANGED CHECKS, not elapsed days, so a source that goes
# quiet slows down and one that wakes up snaps back to base on first change.
UNCHANGED_STREAK_THRESHOLD = 6
BACKOFF_INTERVAL_DAYS = 90
BACKOFF_MAX_DAYS = 180   # never wider: a source we stop looking at is a source
                         # whose staleness we stop noticing.

# The cheap oracles never back off — their whole value is being asked often.
NEVER_BACKOFF = frozenset({"ecfr_titles", "nominations"})

# Failure widens the interval so a dead endpoint is not hammered, but it never
# suppresses the alarm, which is driven by fail_streak.
FAIL_BACKOFF_DAYS = (1, 3, 7, 14)


@dataclass(frozen=True)
class Config:
    project_root: Path
    state_dir: Path
    output_dir: Path
    contact: str | None
    user_agent: str
    cadence_days: dict[str, int] = field(default_factory=dict)
    enabled_sources: tuple[str, ...] = ()
    # circuit breakers (plan §11 R1, R8)
    max_changes_per_run: int = 40
    rowcount_sanity_pct: float = 0.20
    max_coverage_flips_per_run: int = 20
    # LLM governor (plan §8)
    max_usd_per_run: float = 2.00
    anthropic_api_key: str | None = None
    congress_api_key: str | None = None

    @property
    def db_path(self) -> Path:
        return self.state_dir / "fedstruct.sqlite"

    @property
    def raw_dir(self) -> Path:
        return self.state_dir / "raw"

    def cadence(self, source: str) -> int:
        return self.cadence_days.get(source, DEFAULT_CADENCE_DAYS.get(source, 30))


def parse_config(raw: dict, base: Path) -> Config:
    """Build a Config from an already-loaded mapping.

    `base` is the project root; every relative path in the YAML resolves
    against it. Tests call this directly with a dict and a tmp_path.
    """
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    def _path(key: str, default: str) -> Path:
        p = Path(raw.get(key, default))
        return p if p.is_absolute() else base / p

    cadence = dict(DEFAULT_CADENCE_DAYS)
    for k, v in (raw.get("cadence_days") or {}).items():
        if not isinstance(v, int) or v < 1:
            raise ConfigError(f"cadence_days.{k} must be a positive integer, got {v!r}")
        cadence[k] = v

    enabled = raw.get("enabled_sources")
    if enabled is None:
        enabled = ["fr_agencies", "ecfr_agencies", "plum"]
    if not isinstance(enabled, list):
        raise ConfigError("enabled_sources must be a list")

    pct = float(raw.get("rowcount_sanity_pct", 0.20))
    if not 0 < pct < 1:
        raise ConfigError(f"rowcount_sanity_pct must be in (0,1), got {pct}")

    from .http import BROWSER_UA

    return Config(
        project_root=base,
        state_dir=_path("state_dir", "state"),
        output_dir=_path("output_dir", "output"),
        contact=raw.get("contact"),
        user_agent=raw.get("user_agent") or BROWSER_UA,
        cadence_days=cadence,
        enabled_sources=tuple(enabled),
        max_changes_per_run=int(raw.get("max_changes_per_run", 40)),
        rowcount_sanity_pct=pct,
        max_coverage_flips_per_run=int(raw.get("max_coverage_flips_per_run", 20)),
        max_usd_per_run=float(raw.get("max_usd_per_run", 2.00)),
    )


def load_config(path: Path | None = None, *, env: dict | None = None) -> Config:
    """Read config/fedstruct.yaml, then layer in secrets from the environment.

    Secrets are the ONE thing that comes from the environment, and they arrive
    here rather than being read at point of use, so the "nothing downstream
    reads env vars" rule still holds.
    """
    import os

    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(
            f"no config at {path}. Copy config/fedstruct.example.yaml to "
            f"config/fedstruct.yaml and edit it."
        )
    raw = yaml.safe_load(path.read_text()) or {}
    cfg = parse_config(raw, path.resolve().parents[1])

    env = env if env is not None else os.environ
    return replace(
        cfg,
        anthropic_api_key=env.get("ANTHROPIC_API_KEY") or None,
        congress_api_key=env.get("CONGRESS_GOV_API_KEY") or None,
        contact=cfg.contact or env.get("FEDSTRUCT_CONTACT") or None,
    )


def load_dotenv(path: Path) -> None:
    """Twelve-line .env loader; an already-set variable always wins.

    That precedence is deliberate: in GitHub Actions the same names arrive from
    secrets, and a stale committed .env must never shadow them.
    """
    import os

    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        if val and not os.environ.get(key):
            os.environ[key] = val
