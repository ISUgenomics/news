# Topic Brief — Build Sheet (session handoff)

2026-09-17 · written so a fresh session can resume without the prior conversation.

## What this is

Build the pipeline described in `spec.md` as a set of **lib-seed modules** plus thin app glue, using multi-agent workflows (the user opted in with "ultracode"). Design is done. No code has been written. The next action is an approval gate, then the build.

Read in this order:

1. `spec.md` — the product and architecture spec (provider-agnostic, profile-driven). Authoritative.
2. `docs/SEEDS-review.md` — every seed's boundary: purpose, when_to_use, inputs, outputs, deps, must-not-know, public API, docstring, prior-art search results, and the critic's fixes. Generated from `docs/seed-design.json`.
3. This file — decisions, pending approvals, build phases.

## Decisions already made

- **Profiles are the unit of configuration.** `profiles/<name>.yaml`; adding a profile is a YAML file, never a code change. First profile: `isu-ai`.
- **LLM is a pluggable provider** behind the `LLMProvider` Protocol from the code library (`cli-llm-providers`): `claude-cli`, `codex-cli`, `local` (Ollama), optional `anthropic-api`. Prompt-in, text-out; JSON validated in code, not by the API.
- **Every reusable module is a seed** via `/lib-seed` (docstring is the design doc, tests first, stub not mock, harvest scorer must band `strong`). App-only glue stays in `brief/`.
- **Six library features are vendored** with `/lib-use`, not rewritten: `cli-llm-providers`, `ollama-local-llm`, `sqlite-versioned-schema`, `layered-config-overlay`, `secret-redaction`, `secret-scanner`. All `verified` tier. Three report drift `changed` (sqlite-versioned-schema, secret-redaction, secret-scanner); the library copies are the tested ones.
- **Package name** `brief`, CLI `brief ingest | synthesize | deliver | doctor`. `uv`-managed (uv 0.12.15 installed 2026-09-17).
- **Scope of this build:** spec phases 1–3 code plus the pubmed adapter from phase 5, so a second profile works. Phase 4 is audience rollout, not code.

## Pending approvals (ask these first, one AskUserQuestion)

The prior session was interrupted before the user answered. Recommendations in bold.

1. **Seed set.** 11 seeds as listed below with critic fixes applied; `silent-source-check` folded into `db.py` as a function. Alternatives: inline `config-env-interpolate` too (10 seeds), or keep `silent-source-check` as a seed (12). **Recommend: 11.**
2. **Drift.** Proceed vendoring the three `changed` features as-is, or review drift first. **Recommend: proceed.**
3. **Commits.** Not a git repo yet. Init, branch `feat/scaffold`, then: commit per seed staged by path / no commits / single commit at the end. **Recommend: per seed.** Never merge to main without the user.

## The seed set

| Seed | Purpose (one job) | Deps | Harness |
|---|---|---|---|
| `feed-fetch` | RSS/Atom fetch with conditional GET and browser UA; entries as plain dicts plus validators | feedparser | stub_http |
| `page-main-text` | fetch one URL with browser UA, extract main text, cap length (split out of feed-fetch by the critic) | trafilatura (lazy) | stub_http |
| `nsf-award-search` | NSF Award Search v1 by awardee / keyword / PI within a date range | stdlib | stub_http |
| `nih-reporter-search` | NIH RePORTER v2 by org_names or advanced_text_search within a date window | stdlib (copies 7-line POST shape from `ollama-local-llm/provider.py _post`) | stub_http |
| `usaspending-award-search` | USAspending spending_by_award by recipient within a time period | stdlib | stub_http |
| `pubmed-search` | E-utilities esearch + esummary → article dicts keyed by PMID (NOT an RSS URL builder; see corrections) | stdlib | stub_http |
| `keyword-relevance` | any_of / none_of filter with distinct-hit count, whole-word via `\w` lookarounds | stdlib | none |
| `context-packer` | pack ranked items into a char budget from context window / chars-per-token / overhead / reserve; report leftovers | stdlib | none |
| `llm-json-contract` | wrap a `complete(messages)->str` callable: extract first JSON object, validate against a JSON Schema, retry once with the error and the failed reply | jsonschema (lazy) | none |
| `cited-digest-render` | bucketed result → Markdown; item ids → links; drop and report uncited or unknown-id entries; `extra_sections` mapping instead of a hardcoded `watch_list` | stdlib | none |
| `markdown-email` | Markdown → multipart HTML + text via SMTP; injectable msgid; stylesheet constant | markdown (lazy) | stub_smtp (write it: ~40 lines, `smtpd` is gone in 3.12+) |
| `config-env-interpolate` | walk a nested mapping, substitute `${VAR}` and `${VAR:-default}` from an explicit env mapping, error names the key path | stdlib | none |

Prior art: every seed searched three or more phrasings with `lib_search.py`; no whole-feature or part matches except the NIH POST shape and the stub_http harness. Full per-seed detail in `docs/SEEDS-review.md`.

**Cross-seed convention for the four fetchers** (from the critic, apply at write time): `search_<source>(...)`, `build_<query|criteria|request>()`, `normalize_<record>()`, `<Source>SearchError`, kwargs `base_url`, `timeout_s`, `user_agent`; normalized keys `external_id`, `url`, `title`, `body`, `published_at`, `raw` plus source extras; `amount` is the JSON value verbatim; no clock reads inside a seed (explicit dates in, so goldens are deterministic).

## App-only modules (not seeds)

`brief/cli.py`, `brief/models.py` (Item, Profile), `brief/profile.py` (YAML + overlay + interpolate + validate), `brief/db.py` (DDL via sqlite-versioned-schema, content_hash, upsert, window query, `last_seen_by_source`, `silent_sources`), `brief/sources/{rss,nsf,nih,usaspending,pubmed}.py` (20–40 line adapters: params + since → seed kwargs → `list[Item]`), `brief/select.py`, `brief/synthesize.py`, `brief/render.py`, `brief/deliver.py`, `brief/llm/__init__.py` (`get_provider(config)` dispatch), `prompts/system.md`, `prompts/schema.json`.

## Spec corrections to apply before code

- `spec.md` says the PubMed query string becomes the saved-search RSS URL. Wrong: those URLs are minted server-side. The `pubmed` adapter uses E-utilities; a hand-minted PubMed RSS URL is an ordinary `rss` source.
- NSF v1 API omits `abstractText` unless `printFields` is set explicitly. Verify live before pinning the golden URL fixture.
- USAspending `date_type` values and NIH `award_notice_date` vs `project_start_date` semantics: verify against live docs at seed time.
- Line budget: the 800 / 1,500 guidance excludes seeds and vendored code.

## Build phases (each a Workflow; read results between phases)

0. **Scaffold** (inline, no agents): `git init`, branch `feat/scaffold`, `uv init`, `pyproject.toml` (typer, pyyaml, feedparser, trafilatura, markdown, jsonschema; dev: pytest), `CLAUDE.md` with the non-negotiables from spec.md's conventions section, `config.yaml`, `sources.yaml`, `profiles/isu-ai.yaml`, `.gitignore` (data/, .venv/, the dotenv file), `tests/harness/stub_http.py` and `stub_cli.py` copied from `~/AI/codeLibrary/templates/python-feature/tests/harness/`.
1. **Vendor** (one agent per feature, parallel): follow `/lib-use` steps 1–4 for each of the six features into `brief/llm/` and `brief/`; record consumers in the library via a worktree per `/lib-use` steps 5–6 (one agent owns the library worktree, sequential).
2. **Seeds** (one agent per seed, parallel, distinct files so no worktree needed): each agent runs lib-seed Phases 2–7 for its seed: skeleton with approved docstring, pytest skeletons per behavior, implement to green, harvest scorer to `strong`, security pass on new files, append to `.lib/seeds.toml`. Agents must not touch `pyproject.toml` or shared files; report needed deps back instead.
3. **Glue** (sequential or small parallel): app modules, prompts, CLI wiring, `brief doctor`.
4. **Review** (adversarial): finders per dimension (correctness, boundary leaks into seeds, idempotency, secrets), then 3-vote refuters per finding, then fix.
5. **Acceptance**: `uv run pytest`; `brief ingest` against fixtures adds N rows then 0 on rerun; a fixture LLM response with a bad `item_id` is dropped by the renderer; broken JSON triggers exactly one retry; `brief doctor` reports claude-cli, codex-cli, local status; the harvest scorer bands every seed `strong`; `lib_status.py` refreshed.

## Environment facts

- macOS, Python 3.14.7, uv 0.12.15, git 2.50.1. `claude` 2.1.274, `codex` 0.153.4, `ollama` 0.33.2 all installed, so all three providers are testable locally.
- `/Users/andrewseverin/AI/news` is not a git repo yet.
- The user's secrets hook blocks any shell command whose text contains `.env.` — write such files with the Write tool, not heredocs.
- Library tools (run by absolute path from the project):
  - `~/AI/codeLibrary/.venv/bin/python ~/AI/codeLibrary/tools/lib_search.py "<situation>" --top 6`
  - `~/AI/codeLibrary/.venv/bin/python ~/AI/codeLibrary/tools/lib_harvest.py . --json`
  - `~/AI/codeLibrary/.venv/bin/python ~/AI/codeLibrary/tools/lib_drift.py <slug>`
  - `~/AI/codeLibrary/.venv/bin/python ~/AI/codeLibrary/tools/lib_status.py`
- Rules that bind the build: branch before editing, stage by path (never `git add -A`), baseline before changing, prove every new check can fail, no new dependency without a one-line justification.

## Resume instruction

Ask the three pending approvals in one question. On approval, run phase 0 inline, then phases 1–5 as workflows, reading each result before starting the next.
