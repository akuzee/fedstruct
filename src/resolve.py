"""Identity reconciliation — the crosswalk (plan §2, §4).

No government source publishes a mapping between Federal Register slugs, eCFR
slugs, USAspending CGAC codes, and OPM's uppercase free text. Building one is
most of this project's real work, and getting it wrong is how the project fails
quietly.

The governing rule, carried over from the prior scoping study:

    Join on identifiers, never on names. Treat name similarity as producing
    CANDIDATES for adjudication. Error costs are asymmetric — a false merge
    invents edges; a false split fragments one entity while looking clean.

So: four binding methods, in strict descending trust.

    anchor         this id minted the unit. Trivially true.
    source_native  the source published the link itself (FR's own parent_id).
                   Never cross-source.
    crosswalk      two independently-published identifiers agree, 1:1, with no
                   competing claim on either side. The ONLY automatic
                   cross-source binding.
    adjudicated    a human wrote it in config/adjudications.yaml.

Name similarity is not on that list and never binds. `propose()` asserts it.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher

RULE_VERSION = "blocking:v1"

# Fuzzy thresholds. The MARGIN is the load-bearing half: without it, "Office of
# the Inspector General" scores ~0.99 against two dozen units and picks one at
# random. A near-tie is evidence of ambiguity, not evidence of a match.
NAME_MATCH_ACCEPT = 0.92
NAME_MATCH_MARGIN = 0.06
NAME_MATCH_REVIEW = 0.80

# Signals that may bind automatically. Every one is a deterministic agreement
# between two independently-published identifiers — never a similarity score.
AUTO_BIND_SIGNALS = ("slug_exact_1to1",)

# Which source anchors the survivor when a merge happens. Federal Register wins
# because it is the only source that publishes organizational hierarchy.
ANCHOR_PRIORITY = ("fr_agencies", "ecfr_agencies", "usaspending", "plum",
                   "congress", "govman", "wikidata")


@dataclass
class ResolveResult:
    proposed: int = 0
    auto_bound: int = 0
    needs_review: int = 0
    adjudicated: int = 0
    unmerged: int = 0
    blocked_by_never_merge: int = 0
    errors: list[str] | None = None

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if v}
        return d or {"no_changes": True}


# ── candidate generation ───────────────────────────────────────────────────
def propose(conn, now: str) -> ResolveResult:
    """Generate and score cross-source candidates. NEVER merges."""
    res = ResolveResult()
    never = _never_merge_pairs(conn)

    units = {r["id"]: r for r in conn.execute(
        "SELECT id, anchor_source, canonical_name, name_norm FROM units "
        "WHERE merged_into IS NULL")}
    keys = defaultdict(list)          # (scheme, key) -> [unit_id]
    for r in conn.execute("SELECT scheme, key, unit_id FROM unit_keys"):
        keys[(r["scheme"], r["key"])].append(r["unit_id"])

    # Slug indices, per source, from the SOURCE-published slug — not the
    # internal display slug, which is uniquified on collision and would
    # therefore never match across sources.
    slug_of: dict[int, dict[str, str]] = defaultdict(dict)
    slug_owners: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for (scheme, key), ids in keys.items():
        if not scheme.endswith("_slug"):
            continue
        src = scheme[:-5]
        for uid in ids:
            slug_of[uid][src] = key
            slug_owners[src][key].append(uid)

    # Blocking keeps this O(n·k) rather than O(n²) over 2,300 units.
    buckets: dict[str, list[int]] = defaultdict(list)
    for uid, u in units.items():
        for bk in _blocking_keys(u, slug_of.get(uid, {})):
            buckets[bk].append(uid)

    seen: set[tuple[int, int]] = set()
    for group in buckets.values():
        if len(group) < 2:
            continue
        for a, b in _pairs(group):
            ua, ub = units[a], units[b]
            if ua["anchor_source"] == ub["anchor_source"]:
                continue                      # never merge within one source
            pair = (min(a, b), max(a, b))
            if pair in seen or pair in never:
                res.blocked_by_never_merge += int(pair in never)
                continue
            seen.add(pair)

            sig = _signals(ua, ub, slug_of, slug_owners)
            score = _score(sig)
            if score < NAME_MATCH_REVIEW and not any(
                    sig.get(k) for k in AUTO_BIND_SIGNALS):
                continue

            conn.execute(
                "INSERT INTO merge_candidates (left_id, right_id, score, "
                "signals_json, proposed_by, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(left_id, right_id) DO UPDATE "
                "SET score = excluded.score, signals_json = excluded.signals_json, "
                "last_seen = excluded.last_seen",
                (pair[0], pair[1], score, json.dumps(sig, sort_keys=True),
                 RULE_VERSION, now, now))
            res.proposed += 1

    conn.commit()
    res.needs_review = conn.execute(
        "SELECT COUNT(*) c FROM merge_candidates WHERE status='open'").fetchone()["c"]
    return res


def _pairs(group: list[int]):
    g = sorted(group)
    for i in range(len(g)):
        for j in range(i + 1, len(g)):
            yield g[i], g[j]


def _blocking_keys(u, slugs: dict[str, str]) -> list[str]:
    """Cheap keys that bring plausible pairs into the same bucket.

    Blocking decides only what gets COMPARED, never what gets merged, so a
    loose key here costs CPU and cannot cause a bad merge.
    """
    out = [f"slug:{s}" for s in set(slugs.values())]
    n = u["name_norm"] or ""
    if n:
        out.append(f"name:{n}")
        words = [w for w in n.split() if w not in
                 ("of", "the", "and", "for", "united", "states", "department",
                  "office", "bureau", "administration")]
        if words:
            out.append("sig3:" + " ".join(words[:3]))
    return out


def _signals(ua, ub, slug_of, slug_owners) -> dict:
    """Every evidence axis, recorded separately so a human sees WHY."""
    sa, sb = slug_of.get(ua["id"], {}), slug_of.get(ub["id"], {})
    shared = set(sa.values()) & set(sb.values())

    # An exact slug match only counts if NOTHING ELSE claims that slug on
    # either side. A slug owned by two units is ambiguous, not confirming.
    exact_1to1 = False
    for slug in shared:
        owners = {uid for src in slug_owners for uid in slug_owners[src].get(slug, [])}
        if owners == {ua["id"], ub["id"]}:
            exact_1to1 = True
            break

    ratio = SequenceMatcher(None, ua["name_norm"] or "", ub["name_norm"] or "").ratio()
    return {
        "slug_exact_1to1": exact_1to1,
        "slug_shared": sorted(shared),
        "name_exact": (ua["name_norm"] == ub["name_norm"]) and bool(ua["name_norm"]),
        "name_ratio": round(ratio, 4),
        "left": {"id": ua["id"], "source": ua["anchor_source"], "name": ua["canonical_name"]},
        "right": {"id": ub["id"], "source": ub["anchor_source"], "name": ub["canonical_name"]},
    }


def _score(sig: dict) -> float:
    if sig["slug_exact_1to1"]:
        return 1.0
    if sig["name_exact"]:
        return 0.95
    return float(sig["name_ratio"])


# ── binding ────────────────────────────────────────────────────────────────
def apply_auto(conn, now: str) -> ResolveResult:
    """Bind only the candidates carrying a deterministic identifier signal.

    Everything resting on name evidence alone stays `open` — indefinitely, if
    that is how long it takes a human to look at it. An unreconciled unit is
    visible and recoverable; a wrongly merged one is neither.
    """
    res = ResolveResult()
    rows = conn.execute(
        "SELECT * FROM merge_candidates WHERE status='open' ORDER BY score DESC"
    ).fetchall()

    for r in rows:
        sig = json.loads(r["signals_json"])
        if not any(sig.get(k) for k in AUTO_BIND_SIGNALS):
            continue
        _merge(conn, r["left_id"], r["right_id"], method="crosswalk",
               by=RULE_VERSION,
               evidence=f"exact 1:1 slug match on {', '.join(sig['slug_shared'])}")
        conn.execute(
            "UPDATE merge_candidates SET status='auto_bound', bind_method='crosswalk', "
            "resolved_at=? WHERE id=?", (now, r["id"]))
        res.auto_bound += 1

    conn.commit()
    res.needs_review = conn.execute(
        "SELECT COUNT(*) c FROM merge_candidates WHERE status='open'").fetchone()["c"]
    return res


def _merge(conn, a: int, b: int, *, method: str, by: str, evidence: str) -> None:
    """Point the lower-priority unit at the survivor. Never rewrites statements."""
    ua = conn.execute("SELECT * FROM units WHERE id=?", (a,)).fetchone()
    ub = conn.execute("SELECT * FROM units WHERE id=?", (b,)).fetchone()
    survivor, absorbed = (ua, ub) if _rank(ua) <= _rank(ub) else (ub, ua)
    conn.execute(
        "UPDATE units SET merged_into=?, merged_by=?, merge_method=?, "
        "merge_evidence=? WHERE id=?",
        (survivor["id"], by, method, evidence, absorbed["id"]))


def _rank(u) -> int:
    try:
        return ANCHOR_PRIORITY.index(u["anchor_source"])
    except ValueError:
        return len(ANCHOR_PRIORITY)


# ── adjudications: the human in the loop ───────────────────────────────────
def apply_adjudications(conn, path, now: str) -> ResolveResult:
    """Rebuild all rule-set merges, then apply config/adjudications.yaml.

    Clearing first is what makes merges reversible: delete a YAML entry, rerun,
    and the merge is gone. Merge state is a pure function of the rules plus the
    file, never an accumulation of past runs.
    """
    import yaml

    res = ResolveResult(errors=[])

    # Clear EVERY merge, then rebuild: rule merges from the rules, adjudicated
    # merges from the file below. That is what makes a merge reversible by
    # deleting one YAML line — a clear that spared adjudicated rows would make
    # human merges permanent and undeletable, which is the opposite of the
    # intent. Human decisions survive because the file is re-read immediately,
    # not because they were skipped here.
    res.unmerged = conn.execute(
        "UPDATE units SET merged_into=NULL, merged_by=NULL, merge_method=NULL, "
        "merge_evidence=NULL WHERE merged_into IS NOT NULL"
    ).rowcount
    conn.execute("UPDATE merge_candidates SET status='open', resolved_at=NULL "
                 "WHERE status='auto_bound'")
    conn.execute("DELETE FROM adjudications")

    doc = yaml.safe_load(path.read_text()) if path.exists() else None
    doc = doc or {}

    for entry in doc.get("merges") or []:
        aid = _aid(entry)
        try:
            survivor = _resolve_ref(conn, entry["canonical"])
            for ref in entry["absorbs"]:
                absorbed = _resolve_ref(conn, ref)
                if absorbed == survivor:
                    continue
                conn.execute(
                    "UPDATE units SET merged_into=?, merged_by=?, "
                    "merge_method='adjudicated', merge_evidence=? WHERE id=?",
                    (survivor, aid, entry.get("evidence", "").strip(), absorbed))
            _record(conn, aid, "merge", entry, now, None)
            res.adjudicated += 1
        except Exception as exc:
            # A stale reference is recorded, not raised: one bad line must not
            # stop every other decision from applying.
            _record(conn, aid, "merge", entry, now, str(exc))
            res.errors.append(f"merge {entry.get('canonical')}: {exc}")

    for entry in doc.get("never_merge") or []:
        aid = _aid(entry)
        _record(conn, aid, "never_merge", entry, now, None)

    for entry in doc.get("identifiers") or []:
        aid = _aid(entry)
        try:
            uid = _resolve_ref(conn, entry["unit"])
            conn.execute(
                "INSERT INTO unit_keys (scheme, key, unit_id, method, confidence) "
                "VALUES (?,?,?,'adjudicated',1.0) ON CONFLICT(scheme, key) "
                "DO UPDATE SET unit_id=excluded.unit_id, method='adjudicated'",
                (entry["scheme"], entry["key"], uid))
            _record(conn, aid, "identifier", entry, now, None)
            res.adjudicated += 1
        except Exception as exc:
            _record(conn, aid, "identifier", entry, now, str(exc))
            res.errors.append(f"identifier {entry.get('unit')}: {exc}")

    conn.commit()
    return res


def _never_merge_pairs(conn) -> set[tuple[int, int]]:
    """Anti-merges, consulted BEFORE scoring. Nothing overrides these."""
    out = set()
    for r in conn.execute("SELECT payload_json FROM adjudications WHERE kind='never_merge'"):
        try:
            pair = json.loads(r["payload_json"])["pair"]
            a, b = (_resolve_ref(conn, p) for p in pair)
            out.add((min(a, b), max(a, b)))
        except Exception:
            continue
    return out


def _resolve_ref(conn, ref: str) -> int:
    """'fr_agencies:145' or 'slug:environmental-protection-agency' -> unit id."""
    kind, _, val = ref.partition(":")
    if kind == "slug":
        row = conn.execute("SELECT id FROM units WHERE slug=?", (val,)).fetchone()
    elif kind == "id":
        row = conn.execute("SELECT id FROM units WHERE id=?", (int(val),)).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM units WHERE anchor_source=? AND anchor_key=?",
            (kind, val)).fetchone()
    if row is None:
        raise KeyError(f"no unit matches {ref!r}")
    return int(row["id"])


def _aid(entry: dict) -> str:
    return hashlib.sha256(
        json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _record(conn, aid, kind, entry, now, err) -> None:
    conn.execute(
        "INSERT INTO adjudications (id, kind, payload_json, evidence, decided_by, "
        "decided_on, applied_at, apply_error) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET applied_at=excluded.applied_at, "
        "apply_error=excluded.apply_error",
        (aid, kind, json.dumps(entry, default=str), str(entry.get("evidence") or
         entry.get("reason") or "").strip(), str(entry.get("decided_by", "unknown")),
         str(entry.get("decided_on", "")), now, err))


# ── the human-facing half ──────────────────────────────────────────────────
def list_open(conn, limit: int = 20) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT * FROM merge_candidates WHERE status='open' "
            "ORDER BY score DESC LIMIT ?", (limit,)):
        sig = json.loads(r["signals_json"])
        out.append({
            "candidate": r["id"], "score": round(r["score"], 4),
            "left": f"{sig['left']['source']}:{sig['left']['id']} {sig['left']['name']}",
            "right": f"{sig['right']['source']}:{sig['right']['id']} {sig['right']['name']}",
            "name_ratio": sig["name_ratio"], "name_exact": sig["name_exact"],
            "slug_shared": sig["slug_shared"],
        })
    return out


def export_yaml(conn, candidate_id: int) -> str:
    """Print a ready-to-paste YAML block. Deliberately does NOT write config/.

    The tool drafts; the human commits. That keeps "who decided this and why"
    in git history, where an audit trail belongs.
    """
    r = conn.execute("SELECT * FROM merge_candidates WHERE id=?",
                     (candidate_id,)).fetchone()
    if r is None:
        raise KeyError(f"no candidate {candidate_id}")
    sig = json.loads(r["signals_json"])
    left, right = sig["left"], sig["right"]
    lu = conn.execute("SELECT anchor_source, anchor_key FROM units WHERE id=?",
                      (left["id"],)).fetchone()
    ru = conn.execute("SELECT anchor_source, anchor_key FROM units WHERE id=?",
                      (right["id"],)).fetchone()
    keep, drop = (lu, ru) if _rank(lu) <= _rank(ru) else (ru, lu)
    return (
        "merges:\n"
        f"  - canonical: {keep['anchor_source']}:{keep['anchor_key']}\n"
        f"    absorbs:\n"
        f"      - {drop['anchor_source']}:{drop['anchor_key']}\n"
        f"    evidence: >\n"
        f"      {left['name']!r} ({left['source']}) and {right['name']!r} "
        f"({right['source']});\n"
        f"      name ratio {sig['name_ratio']}, exact name match "
        f"{sig['name_exact']}, shared slugs {sig['slug_shared'] or 'none'}.\n"
        f"      REPLACE THIS with the evidence you actually checked.\n"
        f"    decided_by: your@email\n"
        f"    decided_on: YYYY-MM-DD\n"
    )
