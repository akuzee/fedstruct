# Federal Government Structure Explorer — Implementation Plan

## Context

**The need.** There is no free, public, citable way to explore how the US federal government is organized: what the units are, how they nest, what each is authorized to do, who currently holds which post, and where the official sources actually are. Commercial vendors (GovSearch, Leadership Connect) sell a daily-refreshed contacts database with an org chart wrapped around it; their product is *reaching a decision-maker*, not a public reference. The open civic-data world stops at Article I — `unitedstates/congress-legislators` is the most successful US civic dataset there is, and there is no executive-branch equivalent. USAFacts has good per-agency prose but no tree, no statutes, no API, no cross-links.

**What prompted this.** `~/Downloads/interconnectedness-project-scoping.md` evaluated two branches (firm/ownership networks vs. government structure) and narrowed to federal government structure. Its recommended first build was to diff the US Government Manual XML across years and emit a structural changelog, on the theory that the snapshot is well-covered and only the history is missing.

**Why this plan departs from that.** Research this session (all confirmed by live fetch, Sept 2026) found:

1. **GOVMAN cannot support the changelog-first plan.** The XML root is `<GovernmentManual>` → `<Entity EntityId ParentId SortOrder>`, but **all 105 entities in both the 2011 and 2025 editions carry `ParentId="0"`**. The field is vestigial; the file is flat. Sub-units exist only as `<LeaderShipTable><Header>` strings and prose. There is no hierarchy to diff. Its leadership data is a partially-updated ~2017 base — the Dec 31 2025 edition still lists a Forest Service chief who resigned in 2018 and OPIC, which became DFC in 2019. A GOVMAN diff produces exactly the editorial noise the scoping doc feared.

2. **The live snapshot is far more available than the doc assumed.** The Federal Register API publishes a genuine 472-node agency tree with `parent_id` *and* `child_ids`, free and unauthenticated. OPM's PLUM directory (which replaced the paper Plum Book under 5 U.S.C. § 3330f, effective Jan 2026) publishes 15,777 positions with 6,846 current named incumbents. The full chain agency → CFR chapter → `<AUTH>` statute citation → USLM statutory text resolves end to end, free.

**Intended outcome.** A public, static, citable explorer plus a published open dataset — the crosswalk between federal ID systems that no government source and no existing project maintains. History accumulates forward from first run rather than being mined from the past.

**Decisions made this session:**
- **Scope:** wide and shallow first — all agencies, basic layers — not deep on one department.
- **Delivery:** public static site on GitHub Pages **plus** the SQLite/CSV published as an open dataset.
- **Bio layer:** government facts rendered as fact; Wikipedia/Wikidata in a visually separated panel, never blended.
- **LLM:** plumbing only. Crosswalk candidates, defunct classification, diff classification. **Never user-facing prose.** Every string a reader sees traces to a government document.

---

## §1 Verified sources

All confirmed by live fetch. Auth column means HTTP auth, not licensing — everything here is US Government work, public domain.

| Source | Endpoint | Auth | What it gives | Role |
|---|---|---|---|---|
| **Federal Register agencies** | `federalregister.gov/api/v1/agencies.json` | none | 472 agencies; 247 top-level, 225 parented, depth ≤3; `parent_id` + `child_ids` both given; prose `description` with statutory history | **The spine.** Only source of organizational hierarchy |
| **eCFR agencies** | `ecfr.gov/api/admin/v1/agencies.json` | none | 316 agencies with `cfr_references[{title,chapter}]` | Agency ↔ regulation crosswalk |
| **OPM PLUM** | `escs.opm.gov/escs-net/api/pbpub/download-data` | none, CORS-open | CSV, 15,777 rows, 6,846 current incumbents, names + titles + dates + appointment type; 2 levels (174 agencies × 1,442 agency-org pairs) | **The people layer** |
| eCFR versioner | `/api/versioner/v1/full/{date}/title-{n}.xml` | none | `<AUTH>` statute citations, `<SOURCE>` originating FR cite | Statutory authority |
| eCFR structure | `/api/versioner/v1/structure/{date}/title-{n}.json` | none | Nested CFR hierarchy **with byte size per node** | Regulatory-volume proxy |
| US Code | `uscode.house.gov/download/` (PL 119-102) | none | USLM XML; `42 U.S.C. § 7401` → `/us/usc/t42/s7401` is a direct lookup | Statutory text |
| USAspending | `api.usaspending.gov/api/v2/…` | none | 111 toptier agencies, budget authority/obligations, 3-level *financial* hierarchy | Money |
| FR documents | `/api/v1/documents` | none | `agencies[]` with inline `parent_id`, `cfr_references`, `topics[]`, RINs | Coordination graph |
| FR facets | `/api/v1/documents/facets/agency?conditions[term]=…` | none | Ranked agency→count for any query (verified: PFAS → EPA 85, HHS 6, DOE 4, ATSDR 3) | **Function search, pre-computed** |
| congress.gov | `api.congress.gov/v3/nomination` | free api.data.gov key | PAS chains via `"…vice Jed David Kolko, resigned"` | Position history |
| GOVMAN | `govinfo.gov/bulkdata/GOVMAN/{year}/` | none for content | Mission / legal-authority / program **prose**, 241 citable MODS granules | Prose + citation **only** |
| OPM employment | `data.opm.gov/api/v1/files/employment` | none | Quarterly headcount by `AGYSUB` code | Phase 2 (Parquet) |
| Wikidata | SPARQL | none | QIDs, Wikipedia links, party/bio | Enrichment **only** |

**Gotchas that will cost a day each if not handled up front:**
- Federal Register and eCFR **302 datacenter User-Agents** to `unblock.federalregister.gov`. Set a browser UA. A block that returns an empty agency list is the worst possible failure — detect and raise, never follow.
- eCFR returns **HTTP 406** unless the request permits compression. Set `Accept-Encoding` once on the shared session.
- PLUM returned **403 from Akamai** on one fetch attempt. Browser UA plus retry with backoff.
- GOVMAN has **no 2023 edition** and **no published XSD** — the schema is reverse-engineered.

**Known incompleteness that must be surfaced, not hidden:** GAO-26-108164 (Feb 2026) found PLUM missing ≥7 federal entities and ≥130 PAS positions, with inconsistent identifiers and duplicate rows. OPM concurred. The availability registry must be able to represent "this source claims completeness; GAO documented gaps."

---

## §2 The hard problem

No government source publishes a crosswalk between these ID systems, and no existing project maintains one:

```
FR slug/id ↔ eCFR slug ↔ USAspending CGAC + subtier name ↔ PLUM UPPERCASE
  AgencyName|OrganizationName ↔ OPM AGYSUB code ↔ congress.gov org string
  ↔ GOVMAN AgencyName ↔ Wikidata QID
```

Expect **60–70% of total effort here.** The governing principle is carried forward verbatim from the scoping doc's Branch A findings:

> Join on identifiers, never on names; treat name similarity as producing **candidates** for adjudication. Error costs are asymmetric — a false merge invents edges; a false split fragments one entity while looking clean.

This is encoded as a schema constraint and an executable test (§7), not a guideline.

---

## §3 Schema

**Core bet: statements, not a graph.** Entity tables are ID registries holding no attributes. Every fact is a row in `statements` carrying its own provenance and its own two timelines. The org chart is a SQL view. This lets contradictory claims coexist, lets a reader audit any edge back to a filing, and lets the whole store be recomputed from cached raw payloads with no network access.

Follows composter's idiom exactly: `SCHEMA` string constant + `SCHEMA_VERSION` guarded via a `kv` table, in `src/db.py`.

### Tables

- **`runs`** — `run_id, verb, started_at, finished_at, status, llm_cost_usd, http_requests, stmts_written`. Status includes `budget_exceeded`.
- **`sources`** — registry; `authority` is `primary | corroborating | enrichment`.
- **`documents`** — content-addressed. `sha256` UNIQUE; bodies on disk at `state/raw/<sha[:2]>/<sha>.<ext>` so a 75 MiB payload never enters the DB file.
- **`fetch_targets`** — one row per polled thing. Carries `content_sha256` (the gate), `etag`/`last_modified` (cheaper than the gate), `unchanged_streak` and `check_interval_days` (the backoff), `error_streak`.
- **`entities`** — ID registry only. `entity_id` is `unit:fr:145` — provenance legible in the ID. `merged_into` is a **non-destructive pointer**; merges never rewrite statements.
- **`source_identifiers`** — the crosswalk. `PRIMARY KEY (source, id_kind, id_value)` structurally forbids one source ID fanning out to two entities. `binding_method` ∈ `anchor | source_native | identifier | adjudicated` — auditing "which cross-source edges rest on a human guess?" is one `WHERE` clause.
- **`statements`** — the whole point. See below.
- **`merge_candidates`** — `CHECK (left_entity_id < right_entity_id)` gives canonical pair ordering, so no A/B + B/A duplicates. `candidate_id` hashes the sorted pair + rule version, so reruns update rather than duplicate.
- **`adjudications`** — materialized from `config/adjudications.yaml` each `resolve` run, rebuilt from scratch, so deleting a YAML line un-merges.
- **`cfr_nodes`**, **`authority_citations`** — authority attaches to a CFR **node**, not a part (`<AUTH>` appears at part, subpart, and subject-group levels). `raw_text` verbatim always retained, so a parser upgrade needs zero re-fetching.
- **`source_caveats`** — seeded from `config/caveats.yaml`, joined into every derived view.
- **`llm_calls`** — per-call cost ledger.

### The three timelines on `statements`

This is the single most important design decision in the project.

| Columns | Meaning |
|---|---|
| `valid_from` / `valid_to` / `valid_precision` | **World time.** When the fact was true, as a validity interval closed on change (slowly-changing-dimension type 2). `valid_precision` exists because PLUM gives day precision, GOVMAN gives a publication year, and FR prose gives "1971" — without it you silently fabricate precision. |
| `effective_from` | **Stated effective date.** Populated *only* when a Federal Register document or statute states one. Usually NULL — and the changelog must then say "observed," never imply an effective date. |
| `period_start` / `period_end` | **Office term.** The statutory term, identical for everyone holding the post under that authority. **NULL for acting officials** — which is what makes the Federal Vacancies Reform Act distinction representable at all. |
| `observed_first` / `observed_last` / `observed_count` | **Observation time.** When the scraper saw it. Never conflated with world time. (FollowTheMoney conflates these; it is the main reason not to adopt it.) |

**The one line in the schema that matters most:**

```sql
CREATE UNIQUE INDEX idx_stmt_current ON statements(unit_id, predicate, source)
  WHERE valid_to IS NULL;
```

At most one open row per (unit, predicate, source). Every temporal-store bug that has ever existed — double-open intervals, lost closes, resurrected rows — becomes an `IntegrityError` at write time instead of a silently wrong page. The same partial index applies to `unit_edges` on `(child_id, source)`. Note the index is per-*source*: **two sources disagreeing about who a bureau reports to is a representable state and is itself a finding**, not a bug to resolve away.

Hierarchy lives in its own typed `unit_edges` table rather than as a predicate in `statements`, for one reason: `WITH RECURSIVE` over a two-column edge table is readable; over an EAV table it is not.

### Growth: why SCD-2 and not snapshot-per-run

The deciding constraint is not query speed — SQLite handles any of these at this scale. It is **git**, since the DB is committed on every refresh and git stores binary blobs whole. Repo growth is DB size × commits, so DB growth is the only lever.

A full snapshot per run writes ~22,400 rows (3,776 FR + ~1,300 eCFR + ~1,500 USAspending + 15,777 PLUM), or **~268,000 rows/year** at monthly cadence. The actual change rate is far lower: 20–60 real reorganizations/year government-wide, plus a few hundred incumbency changes/month. Realistic SCD-2 growth is **~2,000–4,000 rows/year** — roughly 70–100× less, and, more importantly, growth *proportional to news*. A month where nothing happens should cost near-zero bytes and produce an empty `git diff`.

### The rule that makes that hold: normalize before comparing

Prose churn is what degenerates SCD-2 back into snapshot-per-run. FR rewords a `description` and every agency gets a new row. So predicates carry two independent attributes, and conflating them is the mistake:

```python
# normalize.py
#   `versioned`   -> does a change open a new validity interval at all?
#   `materiality` -> if it changes, is it news?
PREDICATES = {
    # predicate           materiality      versioned
    "exists":            ("structural",    True),
    "parent":            ("structural",    True),
    "name":              ("structural",    True),
    "cfr_chapter":       ("structural",    True),   # gain/loss of regulatory authority
    "short_name":        ("nominal",       True),
    "budget_authority":  ("quantitative",  True),
    "headcount_fte":     ("quantitative",  True),
    "slug":              ("cosmetic",      False),
    "url":               ("cosmetic",      False),
    "description":       ("cosmetic",      False),  # raw_hash only; never a new row
}
```

**Intervals close on `value_norm`, never on `value_raw`.** A capitalization fix, a slug rename, or reworded prose bumps `last_fetch_id`, increments `cosmetic_revisions`, and produces zero rows and zero changelog lines. This kills the bulk of editorial churn *before it becomes a diff at all*, which is far better than filtering it afterward. `normalize_name()` is deliberately not clever — every entry in its abbreviation map is a pair actually observed differing between two sources. A normalizer that guesses is a normalizer that silently merges two real agencies.

### Predicates

A module constant, so new facts never require a migration. Critically, **containment kinds are distinct predicates** — `contains_unit_org` (Federal Register), `contains_unit_financial` (USAspending CGAC/subtier), `contains_unit_advisory`. Wikidata's central failure is flattening these into one `P749`; the 111-toptier vs. 247-top-level impedance mismatch becomes visible in the data rather than papered over. Budget flows to an org unit only through an adjudicated `attributed_budget_of` statement.

### Why not FollowTheMoney

All 70 schemata were checked. `PublicBody` extends `Organization` and **declares zero properties of its own**, and FtM has **no parent-organization relation anywhere** — org-to-org structure would have to be forced through `Ownership` or `Membership`. That is disqualifying for a product that is a tree of units. Its provenance also attributes values to *datasets*, not to clickable documents. Borrow four things and leave the rest: the Position/Occupancy split, office-term vs. individual-tenure dates, `Succession` edges for reorganizations, and the statement row shape.

---

## §4 Identity and adjudication

**Minting.** Every entity has exactly one anchor — the source identifier that created it — and the ID derives from it, so the whole store is reproducible from an empty database. EPA-from-FR is `unit:fr:145`, EPA-from-eCFR is `unit:ecfr:environmental-protection-agency`, EPA-from-USAspending is `unit:usaspending:068`. **All three exist separately until something binds them.** That is the correct default: a false split is recoverable, a false merge is not. PLUM has no IDs at all, so its anchor hashes the natural key `AgencyName|OrganizationName|PositionTitle`.

**The four binding methods, in strict descending trust:**

1. `anchor` — this ID minted the entity.
2. `source_native` — the government source published the link itself. Only FR's own `parent_id`/`child_ids` and eCFR's own `children[]` qualify. Never cross-source.
3. `identifier` — a deterministic identifier corroborates across sources. **The only automatic cross-source binding permitted.** Shared `wikidata_qid`; exact hostname match between a USAspending `congressional_justification_url` and an FR `agency_url`; CFR chapter co-occurrence with zero counterexamples.
4. `adjudicated` — a human wrote it in YAML.

**Name similarity is never a binding method.** It only ever generates candidates. `resolve.py` asserts this and a test asserts the assertion.

**The human loop.** `config/adjudications.yaml` holds four kinds of entry: `merges` (canonical + absorbs + evidence), `never_merge` (anti-merges consulted *before* scoring — e.g. EPA vs. EPA Office of Inspector General, which the blocker will keep proposing and where merging would erase the statutory independence the site exists to show), `identifiers` (hand-attach a source ID no rule can find), and `retractions` (withdraw a bad statement without deleting it).

`adjudicate --export-yaml --candidate <id>` prints a ready-to-paste block **to stdout; it does not write `config/`.** The tool drafts, the human commits. That keeps "who decided this and why" in git history where it belongs.

---

## §5 Layout and verbs

```
src/
├── main.py        argparse subparsers; every cmd_* prints json.dumps(result, indent=2)
├── config.py      config/gov.yaml → frozen Config; nothing else reads YAML or env
├── db.py          SCHEMA, SCHEMA_VERSION, connect(), run ledger, statement upsert
├── ids.py         entity_id minting, stmt_id hashing, anchor priority
├── http.py        browser UA, Accept-Encoding, retries, unblock-302 detection
├── fetch.py       due-ness + sha256 gate + content-addressed document store
├── statements.py  assert_statement(), PREDICATES vocabulary, retract()
├── cite.py        pure-Python USC/CFR citation parser  ← see R8
├── resolve.py     blocking, candidate proposal, identifier auto-bind, merge application
├── adjudicate.py  load/validate adjudications.yaml; YAML draft export
├── llm.py         Anthropic client, PRICING, BudgetExceeded, model tiering
├── prompts.py     prompt constants, JSON schema written inline
├── views.py       SQLite → output/*.json, deterministic
├── status.py      coverage matrix, unresolved counts, staleness, caveats
├── doctor.py      preflight: config, network, UA not blocked, disk
└── sources/
    ├── base.py              Source protocol; knows nothing about storage
    ├── federal_register.py  the spine — only source of contains_unit_org
    ├── ecfr.py              agency ↔ CFR crosswalk + node structure
    ├── ecfr_authority.py    versioner XML → <AUTH> attached to the right node
    ├── plum.py              CSV → Position + Person + occupancy
    ├── fr_documents.py      joint rulemaking → coordination; facets → function search
    ├── usaspending.py       financial hierarchy, own namespace
    ├── usc.py               USLM → citation validation          [phase 2]
    ├── congress.py          nominations → PAS chains            [phase 2]
    ├── govman.py            prose only — predicate allowlist    [phase 2]
    ├── wikidata.py          QIDs + Wikipedia links              [phase 2]
    └── opm_headcount.py     parquet, needs pyarrow              [phase 3, see R11]
```

| Verb | Does |
|---|---|
| `init` | Create DB, seed sources and caveats, enumerate root targets |
| `doctor` | Preflight: config parses, sources reachable, UA not redirected, disk space |
| `fetch [--source S] [--force]` | Poll due targets; sha256 gate; store documents |
| `parse [--source S] [--reparse]` | Documents → statements. Pure, no network |
| `resolve [--propose] [--apply]` | Candidate generation and merge application |
| `adjudicate [--list-open] [--export-yaml]` | Human loop; stdout only |
| `review-links` | Review fuzzy name matches in the 0.80–0.92 band; a reviewed link becomes `asserted` and sticky |
| `register [--audit]` | Rebuild the coverage matrix; `--audit` prints the NA rule-abuse report |
| `enrich [--task …] [--max-usd N]` | The only verb that spends money |
| `build` | statements → `output/*.json` and `output/*.md` |
| `status` | Coverage matrix, unresolved counts, staleness, cost to date |
| `commit-message` | Prints the changelog headline, so the git subject *is* the summary |
| `all` | `fetch → parse → resolve --apply → register → build` — **deliberately excludes `enrich`** |

`all` excluding `enrich` is deliberate: the default composable run is free and offline-safe. Spending money is always an explicit verb.

**Reuse directly** (do not re-derive):
- [`composter/src/db.py`](/Users/adamkuzee/Projects/composter/src/db.py) — the `SCHEMA`/`SCHEMA_VERSION`/kv-guard idiom
- [`composter/src/config.py`](/Users/adamkuzee/Projects/composter/src/config.py) — the `load_config`/`parse_config(raw, base)` split that makes tests config-free
- [`composter/src/sources/base.py`](/Users/adamkuzee/Projects/composter/src/sources/base.py) — the storage-ignorant Source contract
- [`composter/tests/conftest.py`](/Users/adamkuzee/Projects/composter/tests/conftest.py) — the `tmp_path` Env harness
- [`composter/src/alarm.py`](/Users/adamkuzee/Projects/composter/src/alarm.py) — three-tier failure surfacing, notify-only-on-transition
- [`venue-radar/src/llm.py`](/Users/adamkuzee/Projects/venue-radar/src/llm.py) — `PRICING`, `BudgetExceeded`, model tiering, cache_control
- [`venue-radar/src/poll.py`](/Users/adamkuzee/Projects/venue-radar/src/poll.py) — `_is_due()` streak→interval backoff, `last_content_hash` early return
- [`venue-radar/.github/workflows/poll.yml`](/Users/adamkuzee/Projects/venue-radar/.github/workflows/poll.yml) — cron + `workflow_dispatch` + commit-state-back

`requirements.txt`: `requests`, `pyyaml`, `lxml`, `python-dateutil`, `anthropic`. `lxml` earns its slot specifically — `<AUTH>` appears at multiple CFR levels, so attaching authority to the right node needs the XPath `ancestor::` axis, which stdlib `ElementTree` lacks.

---

## §6 Refresh, history, and the availability registry

### History

**Structural history accrues forward from run #1.** The corpus does not exist in usable form (GOVMAN is flat and stale; FR/eCFR/PLUM serve only current state). The README should carry a standing sentence: *this project's structural history begins on its first run; anything before that is attestation, not history.*

**But leadership history is genuinely backfillable, and that's worth exploiting on day one.** PLUM's `IncumbentVacateDate` carries historical incumbencies, and congress.gov nominations carry "vice X, resigned" chains going back decades. So *who ran what, when* backfills to roughly the 1980s on first ingest. **Ingest PLUM's historical incumbencies on run #1, not just the 6,846 current ones.**

GOVMAN contributes existence attestations and nothing more — a separate `attestations(unit_id, corpus, year, granule_url)` table feeding a timeline strip reading "appears in GOVMAN 2011–2022, **2023: not published**, 2024–2025." That missing 2023 is `NOT_CHECKED`, not absence, and it is a concrete instance of the registry's state model earning its keep.

**Run #1 is a release, not a cron tick.** A first run against a hostile-blocked endpoint or a half-finished normalizer permanently degrades every future diff, because there is no re-do. Require `doctor` fully green, `--force` on every source, manual review of the ingested tree before commit, and exempt every run-#1 raw payload from pruning forever.

### Change detection: three tiers, and nothing should sit in tier 2

- **Tier 0 — declared freshness.** The source tells you when it changed. Probe the small marker, download the big thing only if it moved.
- **Tier 1 — content hash.** No marker. Fetch, sha256, skip all parsing if unchanged.
- **Tier 2 — unconditional re-parse.** A bug. A source landing here is a documented regression.

`ecfr_titles.json` is the highest-leverage probe in the project: ~10 KB, and it is the freshness oracle for *all* of eCFR. Check it weekly and eCFR becomes effectively free. `data.opm.gov` listings carry `publishDate` (3 KB, versus a 75 MiB Parquet). USAspending carries `active_fy`/`active_fq`. congress.gov carries `updateDate`. Only FR agencies, eCFR agencies, and PLUM need tier 1 — ~3.3 MB total, and two of those three usually hash-match and parse nothing.

Cadences are **ceilings on staleness, not schedules**. The stated "monthly positions / yearly structure" requirement is about the *product* and the *expensive operations*, not fetch frequency: fetching `agencies.json` costs 500 KB and 300 ms, and parsing costs nothing once the gate says "same." What "yearly" actually buys is permission for the costly work — the OPM Parquet, the USLM zips, the LLM adjudication pass — to run rarely.

Intervals widen on an unchanged streak (6 consecutive → 90 days, capped at 180; never wider, because a source we stop looking at is a source whose staleness we stop noticing). The cheap oracles never back off — their whole value is being asked constantly. Failure widens the interval too, so a dead endpoint isn't hammered, but it never suppresses the alarm, which is driven by `fail_streak`.

### Real change vs. source noise

Layers 1 and 2 are the normalize-before-compare rule and predicate materiality from §3 — those handle the bulk deterministically. Beyond them:

**Quantitative thresholds.** Budget and headcount move every period; without a floor the changelog becomes a ledger. Two floors, ANDed, so neither a large percentage of a tiny number nor a rounding error on a large one promotes itself to news: 10% *and* $50M for budget, 5% *and* 100 FTE for headcount. Quantitative facts are *always stored* — the time series is the point — but only *promoted* above threshold.

**The disappearance problem.** FR has no tombstone and defunct entries sit unmarked, so both "appeared" and "disappeared" are ambiguous. Four rules, in order:

1. **Fetch sanity first.** A fetch whose row count differs from the previous successful one by more than 20% is **quarantined** — ingested nowhere, zero changes emitted, alarm raised. A truncated response and a mass abolition look identical to a differ, and only one of them is real.
2. **Absence needs repetition** — 2 consecutive successful, non-quarantined fetches. Ported from composter's `missing_runs` grace period, for the same reason: absence inferred from one observation turns a transport hiccup into a permanent false claim.
3. **Absence is not abolition.** `lifecycle` moves to `absent_from_source`, never to `abolished`. Promotion requires corroboration: absent from FR for ≥2 runs **and** no current PLUM positions **and** no USAspending obligations this or prior FY **and** no eCFR references. Anything else is `unresolved_absence` and goes to the adjudication queue.
4. **The unit row is never deleted.** An explicit `ALLOWED_TRANSITIONS` map, exactly as in composter's `db.py:29`. `abolished` is not terminal — agencies come back — but leaving it requires a human verdict.

**The credibility firewall.** The LLM never writes to `statements`, `unit_edges`, `unit_keys`, or `coverage`. It writes exactly three columns of `changes`: verdict, confidence, rationale. The fact store stays mechanical and replayable; only the *interpretation* is model-assisted, and it is labelled as such on every rendered line. A verdict citing a URL not in the supplied list is discarded as a hallucination. `verdict_source ∈ {rule, llm, human}` with `verdict_locked` on human calls, and re-adjudication is opt-in per change id — otherwise a model upgrade silently rewrites published history.

**Corroboration is a display attribute.** `changes.corroboration_count` — single-source structural changes render differently from ones two independent sources agree on. Cheap to compute, and the thing that most directly earns trust.

### The changelog as a product

One entry names the **effective date as absent** rather than implying the observation date is one, lists what the change is **not** evidence of, and shows corroborating sources that *did not* change — the strongest available signal that a reorganization was real rather than a data edit. Every entry carries fetch id, sha256, and a link to the raw snapshot.

**Publish what was suppressed.** A collapsed section listing the cosmetic changes filtered out this period, with the rule that filtered each. Showing the noise filter's work is a stronger credibility move than showing only what survived it.

**Publish no-change entries too.** "7 sources checked, 3 unchanged by hash, 3 unchanged by declared marker, 1 not due" is a claim worth making — it is exactly what distinguishes this from a scraper that only speaks when it has something to say.

### Availability registry: darkness is derived, not curated

Separate **facets** (what you want to know) from **probes** (a specific source that can answer it). Without that separation you get a source × unit matrix, which conflates "the government doesn't publish this" with "we happen to ingest this API." Ten facets, capped deliberately: `identity`, `hierarchy`, `regulatory_authority`, `statutory_basis`, `leadership`, `budget`, `headcount`, `web_presence`, `narrative`, `external_identity`.

Each probe declares which facets it is *allowed* to satisfy, which is where verified research becomes an enforced constraint rather than a comment:

```python
GOVMAN_PROSE = Probe(
    name="govman_prose",
    facets=("narrative", "statutory_basis"),   # NOT "leadership" — deliberately.
    # GOVMAN's officials are stale to a ~2017 base. Letting it satisfy `leadership`
    # would turn a nine-year-old name into a green cell. Encoding that here means
    # no future probe can quietly re-enable it.
    scope_claim=None, ...)
```

**Five states, not three.** The two extras are what stop the registry lying:

```python
COVERAGE_STATES = (
    "PRESENT",         # found, cited to a specific fetch
    "ABSENT",          # we looked in a source that CLAIMS to cover this unit's
                       # population, and it is not there
    "NOT_APPLICABLE",  # cannot apply to this kind of unit. Rule-based and cited.
    "NOT_CHECKED",     # the probe never ran here. An admission about US, not them.
    "BLOCKED",         # the probe ran and couldn't get an answer. The failure is
                       # ours or the transport's — attributing it to the agency
                       # would be a false accusation.
)
DEFAULT_STATE = "NOT_CHECKED"   # NOT NOT_APPLICABLE. This one line is the difference
                                # between an honest registry and a flattering one.
```

Three supporting rules:

- **`ABSENT` requires a positive scope claim.** You may only say ABSENT if you can point at a source asserting completeness over this unit's population. A probe without one (Wikidata, the Digital Registry) can report PRESENT, but its absence yields `NOT_CHECKED` and it never enters the denominator.
- **`NOT_APPLICABLE` must be earned by a named, cited rule.** Each is a function whose docstring *is* its citation (e.g. "budget authority is reported at the toptier level under the DATA Act; a bureau with no independent toptier code has no separate line to be absent from"). `coverage.reason` carries the rule name into the UI, and `register --audit` prints a rule-abuse report: any unit with >3 NA facets, any rule firing on >30% of units. If a rule is doing that much work it is hiding an absence.
- **`BLOCKED` is sticky and never downgrades to ABSENT.** Carry the previous PRESENT cell forward with `stale_since` set and confidence × 0.8 for up to 90 days. One USAspending 502 must not make 111 agencies "go dark."

**A coverage flip is a change event.** When a facet rolls PRESENT → ABSENT, `register` writes into the same `changes` table with `kind='went_dark'` and it flows through the same materiality rules, adjudication queue, and changelog renderer. That's one of the most genuinely novel outputs available here, and it's nearly free because both subsystems share the change machinery.

**No single darkness number.** Two ratios with printed denominators, `not_applicable`/`unknown` never folded into either: `Documented: core 3 of 4 · full 6 of 8 applicable · 2 n/a · 0 unknown`. The core four are `identity`, `hierarchy`, `leadership`, `budget` — the ones whose absence means you genuinely cannot answer *what is this, who runs it, who does it report to, what does it spend*. A unit with no Wikidata QID is not dark in any sense a citizen cares about.

### Ops

One workflow, three crons, a `mode` input. Daily (`nominations`, which doubles as the liveness heartbeat), monthly on the 5th (positions plus every cheap gate — DATA Act submissions land early-month), yearly in early February (the deep pass: OPM Parquet, USLM zips, GOVMAN, caveat review). Times are offset from `:00`, which is heavily contended on Actions and gets delayed by tens of minutes. `discover` — the expensive wide operation — is `workflow_dispatch` only, documented "manual, ~2×/year", exactly as venue-radar documents its own.

`concurrency: {group: refresh, cancel-in-progress: false}` is the `flock` equivalent, and `cancel-in-progress: false` is the load-bearing half: a run killed between ingest and commit leaves the DB ahead of the repo. A late run is fine; a half-written one is not. At the DB layer, runs still `running` after 6 hours are marked `orphaned` — a runner can vanish, and each source's ingest being a single transaction means the next run simply redoes the work.

`commit-message` is its own verb so the commit subject *is* the changelog headline: `refresh(poll): 3 structural, 1 rename, 12 cosmetic suppressed; 7 sources, 2 changed`. Then `git log --oneline -- state/` is a free refresh history and `git revert` is a real recovery path.

**Secrets: the scheduled path needs none.** FR, eCFR, PLUM, USAspending, OPM, govinfo, and US Code are all open. `ANTHROPIC_API_KEY` is referenced only by `adjudicate`/`discover`, so a rotated key can't break the monthly run. Worth stating in the README — it's why the thing keeps working when nobody is looking at it. One repo *variable* (not secret) holds a contact User-Agent, which FR and govinfo etiquette want.

**Failure surfacing, ported from composter.** `output/STATUS.md` rewritten only when content changed — and here the payoff is sharper than in composter, because an unchanged status file means `git diff --staged --quiet` succeeds and **no commit happens at all**, so a quiet month produces genuinely empty history rather than 12 no-op commits. Staleness is computed at read time, never stored, so a human opening the repo sees "last successful poll: 94 days ago" even though nothing ran to write it. The alarm replaces `osascript` with a **GitHub Issue** — a better fit for "exactly one notification on the raise transition," because idempotence is queryable: if an open issue labelled `alarm` exists, do nothing (no comment, no reopen); on recovery, close it and delete `ALARM.md`.

**The gap is worse on Actions than on launchd: GitHub disables scheduled workflows after 60 days of repository inactivity.** The system's characteristic failure is stopping quietly. `doctor` therefore checks the scheduler itself — the direct port of composter's `launchctl print` check — asserting `refresh.yml` is `active` (not `disabled_inactivity`) and that the last *scheduled* run started within 1.5× cadence. The daily heartbeat job makes a 60-day dormancy window hard to reach.

---

## §7 Frontend and dataset

**Zero build, matching `map games` / `antipode`.** `views.py` writes deterministic static JSON to `output/`; a vanilla-JS site reads it. No React, no bundler, no server — consistent with every web project in `~/Projects`. Deploy to GitHub Pages.

```
output/
├── manifest.json      run id, per-source retrieved_at, schema version, global caveats
├── units.json         registry: canonical id, label, type, all source IDs, child count
├── edges.json         [{from, to, kind, valid_from, valid_to, provenance_ref}]
├── units/<id>.json    per-unit detail — every statement with source_url,
│                      retrieved_at, extraction_method, confidence
├── people.json        persons with occupancy intervals
├── functions.json     topic → agency counts (FR facets + eCFR hierarchy counts)
├── coordination.json  co-authorship graph from joint rulemakings
├── availability.json  the coverage matrix
├── unresolved.json    open merge candidates — deliberately shipped
└── contested.json     where two sources disagree
```

Three rules make the output honest:

1. **Every fact carries its citation.** A unit page is literally a projection of statement rows, each with `source_url`, `retrieved_at`, `extraction_method`, and `confidence`.
2. **The build is deterministic** — sorted keys, no wall-clock values, stable float formatting. Running `build` twice produces byte-identical files, which is directly testable and enforces "output files are pure derived views."
3. **Not a tree.** Dual-hatted officials and matrixed offices make this a DAG. The builder emits the full edge list plus a spanning tree for default rendering, marking non-tree edges `"secondary": true` so cross-links render rather than being dropped. Any true cycle is detected, excluded, and reported in `manifest.json` — never silently broken.

**Two provenance tiers, structurally separated.** Government facts (name, title, dates, appointment type, PAS confirmation history) render as fact. Wikipedia/Wikidata enrichment (party, prior roles, education) renders in a visually distinct panel with its own attribution, plus a link to the official `.gov` bio page. The boundary is a structural feature of the UI, not a footnote. **No personal data is scraped** — only what the officeholder's own agency publishes, or a link to it.

**Weakest link, not average.** A rendered claim shows the weakest step in its derivation chain: `published` (1.0) → `crosswalk` (0.95) → `derived` (0.90) → `adjudicated` (0.80) → `inferred` (0.60, name similarity) → `asserted` (0.50, human override). Propagation is `MIN(confidence)` along `statements.via_key → unit_keys.confidence`, so a published budget figure attached to a unit through a fuzzy name match renders as **inferred** — which is correct, and which a per-fact confidence model gets wrong. Displayed as one of three bands (`published` / `linked` / `inferred`), never as a number: "0.83" implies precision the pipeline does not have.

**Marking is structural, not a flag.** Each method renders through its *own template*, not one template with `if inferred:`. There is no path that renders a claim without choosing a template, and no template that omits its own marking — so a future contributor cannot add a render path and forget the flag. `output/UNSOURCED.md` lists every `inferred`-or-weaker claim in one place; if that file is long, the product is on thin ice and you can see it at a glance.

**Two UI toggles that do most of the honesty work.** "Shade by darkness" recolours the whole tree by coverage — a map of what the government does not say about itself. "Show only inferred edges" greys out every published relationship and leaves visible exactly the parts of the tree that are guesswork. That second view is the most honest thing the product can show, and it should be one click away, not buried.

**Name matching, made explicit.** The PLUM `OrganizationName` case is the archetype. Join order: published code → exact normalized name → fuzzy above **both** a 0.92 acceptance floor **and** a 0.06 margin over the runner-up → otherwise unlinked and counted in `output/UNMATCHED.md` rather than dropped. The margin is the load-bearing half: without it, "Office of the Inspector General" matches 24 units at 0.99 and picks one at random. Every fuzzy link stores its score and runner-up; `review-links` is the human verb, and a reviewed link becomes `asserted` and sticky.

**Where hand-curation is correct, and why.** `config/caveats.yaml` holds external audit findings (GAO-26-108164; GOVMAN staleness) because an audit finding is by nature not machine-derivable. This is the one deliberate exception to "derived, not curated" — say so in the README so it doesn't look like a slip. Its teeth: a caveat with `downgrades_absent: true` makes any ABSENT cell from that probe render as **"not listed in PLUM"** rather than "no positions," excludes it from the darkest-agencies numerator unless a second probe corroborates, and buckets it separately in the score. Where GAO names a specific entity, that cell becomes `ABSENT_CONFIRMED_EXTERNALLY` — an external audit acting as a data source in its own right, which is a considerably stronger claim than anything the pipeline derives alone.

**Open dataset.** Publish `state/gov.sqlite` plus flat CSV exports as a release artifact. The crosswalk (§2) is the genuinely novel asset — nobody publishes it — and it is arguably more valuable than the site.

---

## §8 Where the LLM is allowed

Decision this session: **plumbing only.** No user-facing prose.

**Forbidden:** hierarchy extraction (FR publishes `parent_id` — asking a model to guess a stated fact is pure downside); autonomous merges (violates §2); extracting anything that is already a CSV column or JSON key; citation parsing (must be deterministic and reproducible — regex, not inference).

**Permitted:**
- **Haiku — `defunct_classify`.** The one genuine gap: FR ships defunct agencies with no tombstone flag, and the only signal is prose. 472 rows, one-time, re-run only when a description's hash changes. The prompt requires a verbatim `evidence_span`, and the parser **rejects the response if the span is not a substring of the input** — turning a hallucination into a discarded row rather than a fabricated fact.
- **Haiku — `diff_classify`.** Structural change vs. editorial churn (§6).
- **Sonnet — `merge_dossier`.** Writes a recommendation for a high-scoring open candidate. Stored in `merge_candidates.llm_recommendation` and **advisory only — no code path lets it change `status`.**

Governor is venue-radar's, verbatim in shape: `PRICING` dict, `max_usd`, `BudgetExceeded`, with two additions — every call writes an `llm_calls` row, and `BudgetExceeded` is caught at the verb level so partial work commits and the run is marked `budget_exceeded` rather than lost. **Steady-state cost after the first full run is $0**: unchanged sources don't parse, and no parse means no LLM call.

---

## §9 Build order

**Phase 1 — wide and shallow (the decision).** Federal Register + eCFR + PLUM, end to end through `build`, deployed. These three exercise every hard mechanic: a clean ID source, a crosswalk with no shared identifiers, and an ID-less string-keyed source. Everything else is additive against a schema that will not need to change to accept it.

Within phase 1, order matters:

1. `db.py` (schema, the two partial unique indexes, `runs`, `fetches`), `fetch.py` with the four documented hostilities, and `doctor` with the failure-signature assertions. **Nothing ingests until `doctor` is green** — run #1 is unrepeatable (R0).
2. `normalize.py` + `ingest.py` + the FR and eCFR sources. One SCD-2 path exercised by two sources before any others exist.
3. `classify.py` — the deterministic layers only. Changelog renders. **No LLM anywhere yet.**
4. PLUM, including the historical incumbency backfill, and the crosswalk/adjudication loop. This is the monthly product.
5. `registry.py` + probes + NA rules. Everything already ingested becomes coverage cells for free.
6. `provenance.py` + `match.py` + the split render templates + `UNSOURCED.md`.
7. The static site and the published dataset.

**Phase 2** — statutory authority chain (eCFR `<AUTH>` → USC), function search UI, budget, GOVMAN prose, Wikidata enrichment, congress.gov PAS history.

**Phase 3** — `adjudicate.py` **last.** By this point the deterministic layers should handle most of the volume, and the residue tells you what the LLM is actually for — rather than reaching for a model to solve a problem a rule would have solved. Then the coordination graph, headcount, and the structural changelog as a standalone product.

Ship the scheduler only once there is something worth scheduling.

**Headline metric: percentage of FR agencies bound to ≥3 sources.** Put it in `status` and on the site. It makes reconciliation progress legible, and because it *falls* when a bad merge is split, it is honest in the direction that matters.

---

## §10 Verification

Tests are pytest against `tmp_path` with a conftest `Env` harness, following composter. **No test touches the network** — `http.py` takes an injected session factory and tests pass a `FakeSession` serving byte-frozen fixtures captured from real responses (12 FR agencies including one 3-deep and one defunct; one eCFR agency with two `cfr_references`; one versioner XML with `<AUTH>` at *both* part and subpart level; 30 PLUM rows including a vacated incumbency and a duplicate; one joint FR document with four agencies).

The load-bearing tests:

| Test | Asserts |
|---|---|
| `test_idempotency.py` | Run `all` twice → identical `stmt_id` set; only `observed_*` changes |
| `test_resolve_no_name_merge.py` | Two entities with identical names and no shared identifier stay **separate**. Encodes §2 as an executable guard |
| `test_gate.py` | Unchanged content → exactly 1 HTTP request, 0 documents, 0 statements, 0 LLM calls |
| `test_blocked.py` | A 302 to `unblock.federalregister.gov` raises `SourceBlocked` and **does not** yield an empty agency list |
| `test_authority_node.py` | `<AUTH>` at subpart level attaches to the subpart, not the part |
| `test_govman_allowlist.py` | GOVMAN emits no hierarchy or people predicates even given fixture XML containing leadership tables |
| `test_adjudicate.py` | Deleting a merge entry un-merges on rerun; `never_merge` beats a 0.99 score |
| `test_views.py` | `build` twice → byte-identical; every edge has provenance |

**`doctor` checks, in full** (green/red/yellow, exit 1 on hard fail — composter's shape):

1. Python 3.13; `SCHEMA_VERSION` matches the `kv` row; `state/` writable; no orphaned `running` rows.
2. **Per-source hostility assertions** — not "did it return 200" but the specific known failure signatures: FR returns 200 and *not* a 302 to `unblock.federalregister.gov`; eCFR returns 200 and not 406 with our `Accept-Encoding`; PLUM returns 200 and not 403. This is the check that turns a silent future breakage into a red line.
3. `refresh.yml` is `active` (not `disabled_inactivity`) and its last *scheduled* run is within 1.5× cadence.
4. **`output/` is a pure derived view** — re-render into memory and compare hashes against the files on disk. A mismatch means someone hand-edited an output file, which breaks the project's central invariant.
5. No claim reaches the renderer with a null fetch id (hard fail).
6. API keys present for the requested mode only; `pyarrow` present (yellow, `deep` mode only).

**End-to-end manual check** after phase 1:
```sh
python -m src.main doctor          # all green before run #1 — it is unrepeatable
python -m src.main all             # fetch → parse → resolve → register → build
python -m src.main status          # coverage matrix; expect open candidates
python -m src.main register --audit  # no NA rule firing on >30% of units
node tools/serve.mjs               # then click EPA → sub-offices → a position →
                                   # its current holder → the source URL for each fact
```
Success at phase 1 means: the tree renders 472 agencies; EPA's page shows its real sub-offices and current appointees; every displayed fact has a clickable government source; the "show only inferred edges" toggle reveals exactly what is guesswork; and `unresolved.json` plus `UNMATCHED.md` honestly show what has not been reconciled.

---

## §11 Risk register

**R0 — Run #1 is unrepeatable.** Structural history begins there and cannot be backfilled. A first run against a blocked endpoint or a half-finished normalizer permanently degrades every future diff. → Treat it as a release, not a cron tick: `doctor` fully green, `--force` all sources, manual review before commit, run-#1 payloads exempt from pruning forever. **Do not let run #1 happen on a schedule.**

**R1 — Identity drift producing a phantom mass reorganization.** FR reassigns `id`s or PLUM changes its `OrganizationName` strings, and the differ reports 400 reorganizations in one morning. Published once, this destroys credibility permanently. → `MAX_CHANGES_PER_RUN = 40`: exceeding it quarantines the entire run — zero changes emitted, nothing committed, alarm raised. This is the descendant of composter's `writer.max_new_per_run`, which that plan called its highest-value safety feature; the same is true here. Paired with the 20% row-count sanity gate.

**R2 — False merge invents edges.** It makes the output look *cleaner*. → Auto-binding requires a deterministic identifier, never a name. `never_merge` consulted before scoring. Merges are pointers, so undo is deleting one YAML line. `test_resolve_no_name_merge.py` fails the build if this ever loosens.

**R3 — False split fragments an entity while looking clean.** A UI showing 900 disconnected units looks like a working product. → `unresolved.json` ships to the frontend and the UI must render it. The §9 headline metric exists for this.

**R4 — `NOT_APPLICABLE` becoming a dumping ground.** The failure that makes the registry flattering and false while looking rigorous — and it is invisible from the output. → `DEFAULT_STATE = "NOT_CHECKED"`, named rule functions with citations in their docstrings, `coverage.reason` surfaced in the UI, and the `register --audit` rule-abuse report.

**R5 — Bot-blocking misread as absence of data.** The subtle version: **a 302 that gets followed returns an HTML page which hashes stably and parses to zero agencies — it looks like a successful, unchanged fetch.** → A redirect off the API host is a *failure*, never a redirect to follow. Browser UA set once; 403 retries then disables the target loudly. **No code path turns a block into an empty result.** `doctor` asserts the specific known failure signatures (FR's 302, eCFR's 406, PLUM's 403) rather than merely checking for HTTP 200 — that is what turns a silent 2028 breakage into a red line.

**R6 — GitHub disables the scheduled workflow after 60 days of repo inactivity.** The system stops and nothing says so, because no run means no status means no alarm. → `doctor`'s workflow-state check, read-time staleness in `STATUS.md`, and the daily heartbeat job.

**R7 — Observation date confused with effective date.** `valid_from` is when *we noticed*, which can be months after a reorganization took effect. Presenting it as an effective date is a factual error at scale. → `effective_from` nullable, populated only from an FR document or statute; changelog prose says "observed" unless it is populated.

**R8 — A source outage flipping a whole column to `ABSENT`.** Because coverage flips are change events, one USAspending 502 would fire the alarm *and* publish 111 "went dark" entries. → `BLOCKED` is sticky with carry-forward; a `register` run flipping more than 20 units quarantines that phase.

**R9 — The darkness score read as an agency-quality judgment.** "EPA is 40% dark" will be quoted without its denominator. → Never a single number; always `core X of 4 · full Y of Z applicable`; and a standing README section, "What this score is not," stating that low coverage often reflects a unit's size or type rather than any choice to be opaque.

**R10 — The unit population has no correct definition.** Every aggregate percentage depends on how many units exist, and FR says 472, eCFR says 316, USAspending says 111. → Publish `n` and the inclusion rule with every aggregate; ship `registry/population.md`; never report a bare percentage.

**R11 — LLM verdict drift across model versions.** Re-running `adjudicate` after a model upgrade silently rewrites published history. → `model` and `rules_version` stamped on every verdict; `verdict_locked` on human calls; re-adjudication opt-in per change id, never bulk.

**R12 — Scope creep into a general temporal graph database.** Every mechanism here generalizes attractively, and generalizing them is a way to spend six months not shipping. → `PREDICATES` is finite and hand-written; no schema-inference layer; new sources map to existing predicates or they don't get ingested.

**R13 — PLUM incompleteness presented as fact.** GAO-26-108164. → `source_caveats` joined into every unit view. The UI renders "not covered by PLUM," never "0 positions."

**R14 — GOVMAN staleness poisoning current facts.** → A hard `allowed_predicates` allowlist on the source class makes hierarchy and people *inexpressible* from GOVMAN, enforced by a test.

**R15 — Financial/organizational impedance mismatch (111 vs. 247).** → Separate entity type and separate predicate; budget reaches org units only via adjudicated attribution.

**R16 — Change-noise reported as news.** → The classify step (§6) gates the changelog. When in doubt, classify as cosmetic; a missed reorganization is recoverable, a false "EPA reorganized" headline is not.

**R17 — `unitedstates/citation` is Node.js, not Python** (verified). It cannot be a `requirements.txt` entry, and `eyecite` targets judicial reporters, not `42 U.S.C.` cites. → Hand-roll `src/cite.py` (~80 lines of regex) for the two verified prose styles (`42 U.S.C. 7401, 7411` and `Secs. 110, 301(a), Clean Air Act (42 U.S.C. 7410, 7601(a))`). Keep `raw_text` verbatim, emit `kind='unparsed'` on a miss, version the parser, track coverage % in `status`.

**R18 — eCFR date-versioning churn.** Nodes are date snapshots; a drifting `as_of` manufactures phantom change every run. → Pin `ecfr.as_of` in config; advance deliberately.

**R19 — Parquet weight from OPM headcount.** 26–75 MiB/file needing `pyarrow`, a large wheel against a deliberately short `requirements.txt`. → Confine rather than reject: `opm_employment` runs **only in `deep` mode**, so the monthly job never imports it and the dependency stays optional. Fetch to a temp dir, aggregate to `(AGYSUB, quarter)`, discard the file; retain only `publishDate` + `sha256` for citation. Never commit the Parquet. Note the intelligence-community and USPS exclusions as caveats.

**R20 — `CivicActions/allusgov` is GPL-3.** Copying or vendoring any of it imposes GPL-3 here. → Do not read its source. Use its published *outputs* only as a spot-check reconciliation oracle, and record that boundary in the README.

**R21 — Schema churn as understanding improves.** → The generic `statements` table absorbs new predicates with zero migration. For real changes, bump `SCHEMA_VERSION` and rebuild via `parse --reparse` from content-addressed `state/raw/` with no network. **Exercise this re-parse path in CI, not just in documentation** — it is what makes the design survivable.

---

## §12 Docs to write alongside

Matching existing conventions: `README.md` (what → setup → verb table → gotchas), `BACKLOG.md` in the established genre ("deferred ideas, with why they were deferred — nothing here is committed-to"), and `IMPLEMENTATION-PLAN.md` declared authoritative with §-numbered sections that code comments cite as `(plan §4.5)`.

Seed `BACKLOG.md` from the scoping doc's deferred items and this session's: historical snapshots mined from pre-2011 GOVMAN PDFs; state and local expansion (the schema is jurisdiction-agnostic for exactly this); office locations on a national map and a 10× more detailed DC map; the SAM.gov Federal Hierarchy API (only office-level source, but rate-limited to 10 requests/day for non-federal users — chase the bulk extract before designing around it); a curated interagency-council registry (~50–150 bodies, exists nowhere, would be the most citable original contribution); and the brand→trademark→assignee pipeline from Branch A, which remains genuinely unclaimed.
