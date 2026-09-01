# Backlog

Ideas raised while building, deliberately not built yet — with the reasoning,
so the decision can be revisited rather than rediscovered. Nothing here is
committed-to.

## Next, and blocking the most value

**Reconcile PLUM to Federal Register.** Right now 252 units are confirmed by two
sources and 462 candidates await review, almost all of them PLUM. Until those
land, the flagship agency pages show correct structure and *no people* — EPA's
page has its CFR chapters and no officeholders, because PLUM's EPA is still a
separate unit. This is the single highest-value piece of work in the project and
it is mostly human adjudication, not code. `adjudicate --export-yaml` exists to
make it fast.

Worth trying first: PLUM's top-level `AgencyName` values are only 174 strings,
and 165 already match a Federal Register unit by exact normalized name. Exact
normalized-name agreement across two independent systems, 1:1 in both
directions, with no competing claim, is arguably a `crosswalk` rather than an
`inferred` link — the same argument that justifies the slug rule. Deferred
because it needs the 165 spot-checked by hand before the rule is trusted, and
because a false merge here invents officeholders, which is the worst error the
project can make.

**`parse --reparse` from `state/raw/` without network.** The plan calls this the
path that makes the whole design survivable (R21) — bump `SCHEMA_VERSION`,
rebuild from content-addressed payloads, no re-fetch. It currently half-works:
raw payloads survive a DB wipe, but `parse` finds them via the `fetches` table,
which does not. Needs a filesystem-walk fallback. Should be exercised in CI, not
just documented.

## Sources not yet ingested

**eCFR `<AUTH>` → US Code.** The whole chain was verified end to end: agency →
CFR chapter → `<AUTH>` statutory citation → USLM identifier → statutory text.
`cfr_chapter` statements already carry `{title, chapter}` qualifiers, so the
join key is in place. Two known traps: `<AUTH>` appears at part, subpart, *and*
subject-group level, so authority attaches to a CFR **node**, not a part; and
the citation text is unstructured prose in at least two styles.

**A citation parser must be hand-rolled.** `unitedstates/citation` is
JavaScript, not Python, so it cannot be a `requirements.txt` entry, and
`eyecite` targets judicial reporters rather than `42 U.S.C.` statutory cites.
~80 lines of regex, with `raw_text` always retained so parser upgrades need no
re-fetch, and a coverage percentage in `status`.

**USAspending budget.** Confirmed available, but it is a *financial* hierarchy
(CGAC/subtier/FPDS), not an organizational one — 111 toptier codes against 247
Federal Register top-level agencies. Must live in its own entity namespace with
its own predicate, and reach an org unit only through an adjudicated
attribution. Silently equating them would corrupt the org chart.

**Federal Register documents → the coordination graph.** Every document lists
its co-signing agencies; a verified example carries DOT + FHWA + FRA + FTA with
three separate RINs. A co-authorship graph over that corpus is hard, dated,
citable evidence of coordination *in practice*. Two agencies citing the same
statute in `<AUTH>` gives coordination *as designed*. The gap between the two is
probably the most interesting thing this project could show.

**Function search.** `GET /api/v1/documents/facets/agency?conditions[term]=PFAS`
returns a ranked agency list already computed by the API (EPA 85, HHS 6, DOE 4,
ATSDR 3, CPSC 3). eCFR's `search/v1/counts/hierarchy` is the regulatory-side
twin. Nobody has put a UI on either. Cheap and novel — deferred only because the
tree had to exist first.

**OPM headcount.** Parquet, 26–75 MiB per quarterly file, requiring `pyarrow`
against a deliberately short `requirements.txt`. Confine rather than reject: run
it in `deep` mode only, aggregate to `(AGYSUB, quarter)` at parse time, discard
the file, never commit it. Note the intelligence-community and USPS exclusions
as caveats.

**GOVMAN prose.** Mission, legal-authority, and program text, plus 241 citable
MODS granules. Must ship with a hard `allowed_predicates` allowlist so its
~2017-era leadership data is *inexpressible*, enforced by a test.

**congress.gov nominations.** PAS officeholder chains via the `"…vice Jed David
Kolko, resigned"` pattern in the description field. Filter to `isCivilian` —
military nominations are enormous in volume and carry no description.

**Wikidata enrichment.** Party, prior roles, education, in a visually separated
panel with its own attribution — never blended with government facts. QIDs are
also the strongest available auto-bind signal. Store with confidence < 1.0 and
never as an anchor: Wikidata is editable by anyone, has only 46 direct children
of the federal-government node, and has no temporal discipline.

## Deferred with reasoning

**The availability registry.** Schema is in place (`coverage`,
`coverage_history`, five states, `DEFAULT_STATE = "NOT_CHECKED"`), and probes
are not written. Deferred until more sources exist — a coverage matrix over
three sources mostly measures which three we happened to ingest, which is
exactly the conflation the facet/probe split is meant to prevent.

**LLM plumbing.** Three sanctioned tasks (defunct classification, diff
classification, merge dossiers), none built. Deferred deliberately: the
deterministic layers should handle most of the volume first, and the residue is
what tells you what a model is actually for. Reaching for one earlier is how you
end up using inference where a rule would have done.

**Structural changelog as a product.** The `changes` table is populated and
classified; the renderer is not written. Needs the suppressed-changes disclosure
and the no-change entry, both of which are credibility features rather than
nice-to-haves.

**`state/raw/` retention policy.** Currently unbounded. At observed change rates
it is roughly 5–7 MB/year, so this is not urgent. When it matters: keep every
changed fetch for 24 months, plus the first fetch of each quarter forever, plus
everything from run #1 forever. Moving it to an orphan `data-archive` branch was
considered and deferred — it doubles the ops surface for a cost that stays under
10 MB/year.

**SAM.gov Federal Hierarchy.** The only office-level source, carrying CGAC and
FPDS codes — precisely the crosswalk keys that would raise the reconciliation
metric. Rate-limited to **10 requests/day** for non-federal users, which makes a
recursive crawl infeasible. Chase the bulk extract at
`sam.gov/data-services/Federal Hierarchy/` before designing around it.

**A curated interagency-council registry.** The CIO Council, the Chief Data
Officers Council, the Interagency Council on Statistical Policy, and ~50–150
others each have a statutory or executive-order basis and a website, and there
is **no machine-readable list of them anywhere**. Bounded hand-curation, and
because it demonstrably does not exist, it would be the most citable original
contribution here. There is no structured source for interagency agreements or
MOUs at all, and GAO says the procurement system is unreliable for it — so do
not promise MOU coverage.

**State and local.** The schema is jurisdiction-agnostic (`units.jurisdiction`)
for exactly this. Roughly 90,000 units of local government, no standardization,
per-site extraction with near-zero reuse, and value decaying with election
cycles. Higher educational value per unit than federal — a citizen can already
find out who runs the EPA and cannot find out who runs their county water
authority — but the incumbent moat is refresh cost, which is recurring. This is
the deferral most likely to be wrong, and it is testable: scrape 20
municipalities, return in 90 days, measure decay.

**Historical snapshots from pre-2011 GOVMAN PDFs.** PDF back to 1935. Would need
OCR-quality extraction from a corpus whose XML era is already flat and stale.
Very expensive, and the output would be attestations rather than hierarchy.

**Office locations on a map.** A national map plus a DC-only map at ~10× the
detail. No location data is ingested yet beyond PLUM's free-text `Location`
column. The 3-D DC map is a separate project.

## Rejected, with reasons

**FollowTheMoney as the schema.** All 70 schemata were checked. `PublicBody`
declares zero properties of its own, and **FtM has no parent-organization
relation at all** — the one thing this product is built on. Org-to-org structure
would have to be forced through `Ownership` or `Membership`. Its provenance also
cites *datasets* rather than clickable documents, and it conflates observation
time with validity time. Four ideas were borrowed instead: the Position/Occupancy
split, office-term vs. individual-tenure dates, `Succession` edges, and the
statement row shape.

**`CivicActions/allusgov` as a dependency.** GPL-3, which would impose GPL-3
here. It already ingests nine of these sources, so it is genuinely tempting —
but its maintainers state the cross-source merge is fuzzy-matched, which would
mean inheriting a probabilistic join as ground truth. Do not read its source;
use its published outputs only as a spot-check reconciliation oracle, and record
that boundary.

**Merging on name similarity.** See the README. This is the project's central
rule, not a preference.
