# Topic Brief — Build Record

Built 2026-09-17 on branch `feat/scaffold`. 14 commits, nothing merged to `main`.

`spec.md` is the product spec and stays authoritative. `CLAUDE.md` holds the ten rules the
code must satisfy. This file records what was built, what it cost, and what is left.

## What exists

A working pipeline, verified end to end against live public sources and a real model.

```
uv sync
uv run brief doctor                      # which providers are reachable
uv run brief ingest                      # every source any enabled profile references
uv run brief synthesize --profile isu-ai # or --all
uv run brief deliver    --profile isu-ai # or --all
```

Two live runs produced complete briefs from NIH Guide feeds through `claude-cli`: every
claim cited its source, activity codes and notice numbers came through verbatim, and the
model correctly routed policy items to the watch list rather than inventing a funding call.
A second profile with different sections and a different provenance line ran through the
same code with no changes, which is the abstraction's acceptance test.

**749 tests, 8.7 seconds, no network.** Every test stubs rather than mocks: real sockets
through `stub_http` and a new `stub_smtp`, real subprocesses through `stub_cli`.

## Shape

Two zones, and the boundary between them is the point.

**`src/brief/lib/` — 12 seeds.** Written here, shaped to graduate into `~/AI/codeLibrary`.
Nothing there imports from `brief/`. Approved boundaries in `docs/seeds/`, register in
`.lib/seeds.toml`.

| Seed | Job | Deps | Band |
|---|---|---|---|
| `feed_fetch` | RSS/Atom with conditional GET and a browser UA | feedparser | strong (16) |
| `page_main_text` | one page, boilerplate stripped, capped | trafilatura | strong (16) |
| `nsf_award_search` | NSF v1 by awardee, keyword, or PI | — | strong (22) |
| `nih_reporter_search` | RePORTER v2 by institution or text | — | strong (22) |
| `usaspending_award_search` | spending_by_award by recipient or keyword | — | strong (22) |
| `pubmed_search` | E-utilities esearch + esummary | — | strong (19) |
| `keyword_relevance` | any_of/none_of filter with a distinct-hit count | — | strong (22) |
| `context_packer` | fit ranked items to a budget, report the remainder | — | strong (16) |
| `llm_json_contract` | text completion → schema-valid JSON, one retry | jsonschema | possible (10) |
| `cited_digest_render` | bucketed result → Markdown, uncited entries dropped | — | strong (16) |
| `markdown_email` | Markdown → multipart HTML + text over SMTP | markdown | strong (16) |
| `config_env_interpolate` | `${VAR}` resolution naming the missing key path | — | strong (22) |

**`src/brief/vendor/` — 6 features** copied from codeLibrary, never hand-edited, provenance
in `.lib/vendored.json`: `cli-llm-providers`, `ollama-local-llm`, `sqlite-versioned-schema`,
`layered-config-overlay`, `secret-redaction`, `secret-scanner`.

**Everything else in `src/brief/`** is app glue: `cli`, `db`, `profile`, `select`,
`synthesize`, `render`, `deliver`, `models`, `llm/`, `sources/`. It imports `brief.models`
on purpose. That layer is what keeps the seeds free of app types, so the "entangled" list in
`.lib/STATUS.md` is expected, not a backlog.

## How it was built

Three phases, each a multi-agent workflow, with the results read between them.

1. **Design** — 11 agents drafted seed boundaries and searched the library for prior art;
   one checked drift on the features to vendor; a critic reviewed the whole decomposition.
   It split `feed-fetch` in two, folded `silent-source-check` into `db.py` as too small for
   a module, and added `config-env-interpolate`. Output: `docs/seeds/`, `docs/SEEDS-review.md`.
2. **Seeds** — 12 agents implemented TDD from the approved boundaries, then 12 more
   adversarially reviewed and fixed their own seed. 89 defects found and fixed: 22
   correctness, 20 vacuous tests, 16 contract gaps, 11 security, 11 docstring drift, 9
   boundary leaks.
3. **Review** — 5 reviewers over the app glue across separate dimensions, then 3
   independent skeptics per finding. 36 distinct findings; 21 survived refutation and were
   fixed.

## What the reviews actually caught

The sharpest ones, because they are the argument for having run them:

- **`feed_fetch`'s default transport accepted any urllib scheme**, so a `file://` URL in a
  YAML config read local disk.
- **`parse_feed_bytes` was documented pure but handed a `str` to feedparser**, which opens it
  as a resource — defeating the module's central claim that its own HTTP path is bypassed so
  the headers stay ours.
- **Two profiles querying one source with different parameters saw each other's rows.** A
  maize award to another university would have landed in the Iowa State brief. Reproduced by
  construction before fixing.
- **`Delivery.subject` read a slots member descriptor**, so a profile without an explicit
  subject would have emailed a Python repr as its subject line.
- **`any_of: genome`** — natural YAML — became one keyword per character and matched almost
  everything.
- **A brief whose every entry failed the citation check was delivered as a real brief** with
  a title and nothing under it, which reads as a quiet week.
- **Feed cache validators never round-tripped**, so conditional fetch could never fire.
  Now verified live: the second poll of a real feed returns 0 entries via 304.
- **Two real PI email addresses**, captured from a live NSF response, were sitting in a
  committed fixture. Found by the seed review, fixed by hand.

## Ground truth

Every safety rule was proven able to fail before being trusted. Planting the violation and
watching a test go red, then reverting:

| Mutation | Result |
|---|---|
| dedup ignores whitespace normalization | red |
| body cap cuts bytes mid-character | red |
| `retries=0` on a bad model reply | red, 3 tests |
| bucket order taken from the model | red |
| already-sent check removed | red |
| unknown-id citation check neutered | red, 3 tests |
| `none_of` ignored | red |
| archive written after the send | red |
| refused recipients not reported | red |
| deep merge replaced with assignment | red, 2 tests |
| unknown source name accepted | red |
| fetch deduplication dropped | red |
| adapter drops `raw` | red |
| `sources.yaml` wins over profile params | red |
| thin entry discarded when its page 404s | red |
| unknown provider silently defaults | red, 2 tests |
| `require_provider` returns an unusable one | red |
| `describe` lets a probe exception escape | red |

Three mutations did **not** go red, and each exposed a real gap rather than vindicating the
code:

- The future-timestamp guard in `silent_sources` was dead code. A future time cannot precede
  the cutoff, so the branch never changed the outcome. Removed, test kept as a behavior pin.
- The `none_of` assertion was vacuous: the excluded fixture item failed `any_of` anyway, so
  the exclusion list did no work. Fixture fixed.
- Three edits during the review-fix pass silently failed to apply because a formatter had
  reflowed the target across lines. Each time a test caught it. An unapplied mutation is
  indistinguishable from a vacuous test, so the edit has to be verified, not assumed.

## Library bookkeeping

Consumers recorded for all six vendored features, and two tool defects filed against
`lib_harvest.py`, both measured in this repo. Committed on branch `chore/lib-use-news` in
`~/AI/codeLibrary` via an isolated worktree; the library's own working tree was not touched,
apart from one additive line `lib_status.py` wrote to `repos.json` registering this project.

- **`stdlib-dataclass-scored-as-framework-coupling`** — the `-6` decorated-def penalty counts
  `@dataclass`. Removing the one `@dataclass(frozen=True)` from `llm_json_contract` raises it
  from `possible (10)` to `strong (16)`, exactly `+6`. The cheapest route to the gate is
  therefore to hand-write `__init__` and `__eq__`, which is worse code. The band is accepted
  with that reason recorded rather than gamed.
- **`sibling-tests-invisible-when-tests-are-in-subdirectories`** — `sibling_test_stems()`
  prunes at the first `tests/` directory, so `tests/<group>/test_x.py` is never seen. 12 seeds
  with 31–80 tests each scored "no sibling test file". Flattening the tree moved 11 of 12
  from `possible` to `strong` with no code change.

## Not done

- **Not deployed.** No Docker Compose file, no changedetection.io instance, no cron. Spec
  phase 3 onward.
- **`anthropic-api` provider is not implemented.** Asking for it raises a message saying so
  and naming what does work, rather than falling back to a different model.
- **The ISU feed URLs in `sources.yaml` are unverified** and marked `verify: true`. The
  `verify` flag is read by no code yet — the review flagged that, and it is left as a
  deliberate to-do rather than a silent lie.
- **`brief doctor` does not check source reachability**, only providers.
- **Award API request shapes were confirmed against live documentation, not live calls.**
  Tests run on golden fixtures. The first real `nsf`, `nih`, `usaspending` and `pubmed`
  fetches should be watched.
- **`briefs/` is written but not committed by the pipeline.** The spec calls for a git
  archive; today that is an operator step.
- **`--week` on synthesize selects items by the current 7-day window**, not by the requested
  week. Regenerating an old week with `--week` will pick today's items. Flagged by the review,
  not fixed; it needs a decision about what "regenerate last week" should mean.

## Next

1. Verify the three ISU feed URLs, or convert them to changedetection.io watches.
2. Decide where the signed-in CLI lives, since it decides whether `brief` can run in a
   container, and check whether the subscription terms permit an unattended scheduled job.
3. Deploy phase 3: Compose, changedetection.io, the Regents PDFs, cron.
4. Send week one to yourself, read it, then widen to one friendly reader.
