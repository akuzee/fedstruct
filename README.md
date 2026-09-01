# fedstruct

An interactive, citable map of how the US federal government is organized —
what the units are, how they nest, what each is authorized to do, and who
currently holds which post. Every fact links to the government document it came
from.

There is no free, public equivalent of this. Commercial vendors (GovSearch,
Leadership Connect) sell a daily-refreshed contacts database with an org chart
wrapped around it; their product is reaching a decision-maker, not a public
reference. The open civic-data world stops at Article I — `congress-legislators`
is the most successful US civic dataset there is, and it has no executive-branch
counterpart.

`IMPLEMENTATION-PLAN.md` is authoritative; this file is the operating manual.
Code comments cite it by section, e.g. `(plan §6)`.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp config/fedstruct.example.yaml config/fedstruct.yaml   # edit `contact`
```

No API keys are needed. Every phase-1 source is open, which is why the
scheduled refresh keeps working when nobody is looking at it.

## Run

```sh
python -m src.main doctor     # preflight — run this BEFORE the first ingest
python -m src.main init       # create the DB, seed sources and caveats
python -m src.main all        # fetch -> parse -> resolve -> build
node tools/serve.mjs          # http://localhost:8031/
```

| Verb | Does |
|---|---|
| `init` | Create the database; seed sources and `config/caveats.yaml` |
| `doctor` | Preflight. Asserts each source's known failure signature, not just HTTP 200 |
| `fetch` | Poll due sources through the sha256 gate |
| `parse` | Turn fetched payloads into statements. Pure; no network |
| `resolve --apply` | Propose identity links; bind only those with a deterministic identifier |
| `adjudicate` | The human loop. `--export-yaml <id>` drafts a block; prints to stdout only |
| `build` | statements → `output/*.json`. Deterministic |
| `status` | Staleness, counts, the reconciliation metric |
| `commit-message` | The changelog headline, used as the git subject |
| `all` | `fetch → parse → resolve → build`. Never spends money |

## Project layout

```
src/
  db.py          SCHEMA + SCHEMA_VERSION; the ledger. Read this first
  normalize.py   the predicate table and name normalization
  http.py        every documented source hostility, handled once
  fetch.py       due-ness, backoff, the sha256 gate, the payload store
  ingest.py      applying records as validity intervals; the circuit breaker
  resolve.py     the crosswalk — candidates, binding, adjudication
  views.py       SQLite -> deterministic static JSON
  doctor.py      preflight
  sources/       one module per source; knows nothing about storage
config/
  fedstruct.yaml     cadence, circuit breakers  (gitignored; .example is committed)
  adjudications.yaml identity decisions — the human in the loop
  caveats.yaml       known source incompleteness, e.g. the GAO finding on PLUM
site/index.html      zero-build vanilla JS; reads output/*.json
state/               the SQLite ledger + content-addressed raw payloads
output/              pure derived views. Safe to delete; `build` recreates them
```

## What this does and does not claim

**Structural history begins at this project's first run.** The sources publish
only current state, so history accumulates forward and cannot be mined
backwards. The US Government Manual looks like it should provide it and does
not: its XML is flat (all 105 entities carry `ParentId="0"`) and its officials
track a roughly 2017 base — the Dec 2025 edition still lists a Forest Service
chief who resigned in 2018 and an agency abolished in 2017.

**Officeholder history is the exception.** OPM PLUM retains vacated
incumbencies, so tenure data predates run #1.

**Absence in a source is not absence in the world.** GAO-26-108164 (Feb 2026)
found PLUM missing at least 7 federal entities and 130 Senate-confirmed
positions. Every page renders "not listed in PLUM", never "no positions", and
`config/caveats.yaml` is what gives that wording teeth.

**Unreconciled units are shown, not hidden.** Right now 252 units are confirmed
by two independent sources and several hundred identity links still await human
review. `output/unresolved.json` ships that to the reader, because a UI showing
only what reconciled looks finished when it isn't.

## The one rule that matters

> Join on identifiers, never on names. Treat name similarity as producing
> **candidates** for adjudication.

Error costs are asymmetric. A false merge invents relationships that do not
exist and looks *cleaner* than the truth. A false split fragments one agency and
is visible and recoverable. So `resolve` binds automatically only on a
deterministic identifier agreeing 1:1 across two independently-published
government systems; everything else waits in `config/adjudications.yaml`.

`tests/test_resolve.py::test_identical_names_without_a_shared_identifier_do_not_merge`
encodes that rule as an executable guard so it cannot be loosened by accident.

## Gotchas

- **`doctor` before run #1.** Run #1 is unrepeatable — history starts there and
  there is no re-do.
- **Federal Register and eCFR block datacenter IPs.** They 302 to
  `unblock.federalregister.gov`, and *following* that redirect returns HTML that
  hashes stably and parses to zero agencies — i.e. it looks like a successful,
  unchanged fetch. `http.py` treats a redirect off the API host as a hard
  failure. This never reproduces from a laptop and always reproduces on a
  GitHub Actions runner.
- **eCFR returns 406** without a compression-permitting `Accept-Encoding`. Set
  once on the shared session.
- **PLUM has no identifiers at all** — only uppercase free text — so nothing
  from it binds automatically.
- **`git log --oneline -- state/`** is a free, human-readable refresh history,
  and `git revert` is a real recovery path for a bad run.

## Tests

```sh
python -m pytest tests/ -q
```

No test touches the network: `http.Session` is injected, and tests pass a
`FakeSession` serving byte-frozen fixtures trimmed from real responses.
