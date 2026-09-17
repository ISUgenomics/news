# How it was built

2026-09-17, one day, 38 commits on `feat/scaffold`. Nothing merged to `main`.

This is the process record. `ARCHITECTURE.md` is what the thing *is*; this is how it
got that way, and which decisions were made by measurement rather than by assertion.

## The pipeline that produced the pipeline

```
   spec.md ──────────────────────────────────────────────────────┐
   (product spec, rewritten first: profiles as the unit of        │
    configuration, provider made pluggable)                       │
                                                                  ▼
  ┌──────────────── PHASE 1 · DESIGN ─────────────────────────────────────┐
  │                                                                        │
  │   11 agents, one per candidate seed                                    │
  │      ├─ draft the boundary in house style                              │
  │      ├─ search codeLibrary for prior art, 3+ phrasings each            │
  │      └─ report: seed / use-existing / copy-a-part                      │
  │   1 agent: drift-check the 6 features to vendor                        │
  │   1 critic: attack the whole decomposition                             │
  │                                                                        │
  │   OUTCOME  split feed-fetch in two · folded silent-source-check into   │
  │            app code as too small · added config-env-interpolate        │
  │            → 12 approved boundaries in docs/seeds/                     │
  └────────────────────────────────┬───────────────────────────────────────┘
                                   │  human gate: boundaries approved
                                   ▼
  ┌──────────────── PHASE 2 · SEEDS ──────────────────────────────────────┐
  │                                                                        │
  │   12 agents implement TDD from the approved boundary                   │
  │              │                                                         │
  │              ▼  pipeline, not barrier: each seed verifies as soon      │
  │   12 agents adversarially review and FIX their own seed                │
  │                                                                        │
  │   OUTCOME  89 defects found and fixed                                  │
  │            22 correctness · 20 vacuous tests · 16 contract gaps        │
  │            11 security · 11 docstring drift · 9 boundary leaks         │
  └────────────────────────────────┬───────────────────────────────────────┘
                                   ▼
  ┌──────────────── PHASE 3 · GLUE (written directly) ────────────────────┐
  │   db · profile · select · synthesize · render · deliver · cli          │
  │   adapters, 20-40 lines each, thin by rule                             │
  └────────────────────────────────┬───────────────────────────────────────┘
                                   ▼
  ┌──────────────── PHASE 4 · REVIEW ─────────────────────────────────────┐
  │                                                                        │
  │   5 reviewers, one per dimension, over the glue only                   │
  │      idempotency · boundary · contract · robustness · coverage         │
  │              │                                                         │
  │              ▼  3 independent skeptics per finding, majority refutes   │
  │   36 findings raised → 21 survived → all fixed                         │
  │                                                                        │
  │   CAVEAT  the review ran WHILE I was fixing, so both its lists were    │
  │           stale. 10 "survivors" were already fixed; some "dismissed"   │
  │           only because a refuter read repaired code. Every one was     │
  │           re-checked against current source rather than trusted.       │
  └────────────────────────────────┬───────────────────────────────────────┘
                                   ▼
  ┌──────────────── PHASE 5 · LIVE, REPEATEDLY ───────────────────────────┐
  │   Real feeds, real APIs, a real model. This is where most of the       │
  │   genuinely important defects were found — see below.                  │
  └────────────────────────────────────────────────────────────────────────┘
```

## Where the defects actually came from

```
                                    found by
                                       │
        ┌──────────────────────────────┼──────────────────────────────┐
        ▼                              ▼                              ▼
   ┌──────────┐                  ┌──────────┐                  ┌──────────┐
   │  AGENT   │                  │  TESTS   │                  │ RUNNING  │
   │  REVIEW  │                  │          │                  │   IT     │
   └──────────┘                  └──────────┘                  └──────────┘
    89 in seeds                   mutation testing              the sharpest ones
    21 in glue                    caught 3 of MY OWN
                                  vacuous tests
```

The pattern worth keeping: **agent review found breadth, running it found depth.**
Every one of these came from a live run, not from a test suite that was green:

| found by running it | what it was |
|---|---|
| two ISU feeds 404 | the spec's URLs were guesses; both sites are Drupal and declare no feed |
| NSF returned 159 institutions | `awardeeName` is not a filter — it ORs the words, so "University" matches everyone |
| awards dated 2027 | USAspending's `published_at` was the project START, not the obligation |
| the silence alert cried wolf | it grouped by source name while ingest stored by source key |
| `239160.0` in a brief | a JSON number reaching the page with its float tail |
| the model said "no PI named" | the seeds extracted the PI; the app dropped it before the prompt |
| 83% of an awards article lost | the storage cap, measured by refetching the article whole |
| the weekly brief filled with 2024 | the deep historical ingest made every old award "new to us" |

## Decisions made by measurement, not assertion

Each of these started as an assumption and was changed by a number.

```
  ASSUMED                          MEASURED                        RESULT
  ───────────────────────────────────────────────────────────────────────────
  chunk long articles to fit  →  41 of 43 dropped items are   →  did not build it
                                 under 4,300 chars; chunking
                                 touches none of them

  a bigger context window     →  sends 59 of 59 instead of    →  raised it, once
  only makes it longer           21; "longer" was only bad        the owner said
                                 because I'd fixed 600 words      coverage > brevity

  seed + temperature gives    →  5 of 6 runs byte-identical,  →  shipped, described
  reproducible output            1 differed by one comma          as "stable, not
                                 (Metal is not bit-exact)         bit-identical"

  refetch to add a column     →  facts are a pure function    →  brief reindex:
                                 of raw_json, already stored      0.24s, no network

  the local model is too      →  zero fabricated figures or   →  made it the default
  weak to be trusted             names across a whole brief
```

## Rules that earned their keep

From `CLAUDE.md`, the ones that actually caught something:

- **Rule 2, no institution named in code.** Caught me writing "Iowa State University"
  into a seed's docstring as an example, minutes after adding the guard that found it.
- **Rule 3, a boundary change is a decision.** Forced three boundary documents to be
  revised in the same commit as their code, rather than drifting.
- **Rule 4, never hand-edit vendor.** Turned a two-line convenience into an upstream
  contribution with its own tests, now available to every other consumer.
- **Rule 5, adapters are thin.** The review caught a fix I had put in `select.py` that
  belonged in the adapter.
- **Rule 7, plant the violation.** Three of my own tests passed against deliberately
  broken code. One was reaching the live network and passing on the DNS failure.

## The thing that kept recurring

A test that checks a helper, while nothing checks that anything *calls* it.

```
   test ──> helper          ✓ green, and worthless
              ▲
              │  nobody verifies this edge exists
              │
   caller ────┘
```

It happened four times: the link neutraliser, the publication-age bound, the redaction
step, and the sampler options. Each time the fix was a test that drives the real path.
It is the failure mode to watch for in this codebase.

## What the day cost

```
  911 tests, ~10 seconds       672 of them belong to the 14 seeds
  38 commits                   nothing on main
  14 seeds, 13 band `strong`   ready to graduate when a second project wants them
  6 vendored features          1 improved and contributed back upstream
  2 library tool defects filed measured in this repo, with reproductions
  12 weeks of briefs           40 cited entries, 0 uncited claims shipped
```
