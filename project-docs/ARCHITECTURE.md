# Architecture

Generated from a scan of the code on 2026-09-17. Nothing here is aspirational — every
box exists as a module, every number was measured.

## System overview

```
┌──────────────────────────────── topic-brief ─────────────────────────────────┐
│                                                                               │
│   PUBLIC SOURCES                   ONE SQLITE FILE            ONE MODEL CALL  │
│                                                                per profile    │
│   ┌──────────────┐                                              per week      │
│   │ RSS / Atom   │──┐                                                         │
│   │ 3 ISU feeds  │  │                                                         │
│   └──────────────┘  │                                                         │
│   ┌──────────────┐  │   ┌─────────────┐      ┌──────────────┐                │
│   │ NSF award    │──┼──>│   ingest    │─────>│  items.db    │                │
│   │ NIH RePORTER │  │   │             │      │              │                │
│   │ USAspending  │  │   │ dedup on    │      │ items        │                │
│   │ PubMed       │  │   │ source_key  │      │ briefs       │                │
│   └──────────────┘  │   │ + hash      │      │ source_state │                │
│   ┌──────────────┐  │   └─────────────┘      └──────┬───────┘                │
│   │changedetection──┘                               │                        │
│   │ (not running)│                                  │                        │
│   └──────────────┘                                  │                        │
│                                                     ▼                        │
│                                            ┌─────────────────┐               │
│   profiles/isu-ai.yaml ───── shapes ──────>│     select      │               │
│     topic, sources, buckets,               │                 │               │
│     audience, recipients,                  │ window → filter │               │
│     provider, max_words                    │ → rank → redact │               │
│                                            │ → pack          │               │
│                                            └────────┬────────┘               │
│                                                     │                        │
│                                                     ▼                        │
│                                            ┌─────────────────┐               │
│                                            │   synthesize    │               │
│                                            │                 │               │
│                                            │ prompt from the │               │
│                                            │ profile; CONSTRAIN               │
│                                            │ the decoding if │               │
│                                            │ it can; validate│               │
│                                            │ retry ONCE      │               │
│                                            └────────┬────────┘               │
│                            LLMProvider Protocol     │                        │
│              ┌──────────────┬───────────────┬───────┘                        │
│              ▼              ▼               ▼                                │
│        ┌──────────┐  ┌────────────┐  ┌─────────────┐                         │
│        │  local   │  │ claude-cli │  │  codex-cli  │   anthropic-api         │
│        │ (Ollama) │  │            │  │             │   (not implemented;     │
│        │ DEFAULT  │  │            │  │             │    says so, no fallback)│
│        └──────────┘  └────────────┘  └─────────────┘                         │
│                                                     │                        │
│                                                     ▼                        │
│                                            ┌─────────────────┐               │
│                                            │     render      │               │
│                                            │                 │               │
│                                            │ positions → ids │               │
│                                            │ DROP uncited    │               │
│                                            │ neutralise links│               │
│                                            └────────┬────────┘               │
│                                                     │                        │
│                                      ┌──────────────┴──────────────┐         │
│                                      ▼                             ▼         │
│                            ┌──────────────────┐         ┌──────────────────┐ │
│                            │ briefs/<profile>/│         │   SMTP relay     │ │
│                            │   <week>.md      │         │  (archive FIRST, │ │
│                            │   the archive    │         │   then send)     │ │
│                            └──────────────────┘         └──────────────────┘ │
│                                                                               │
└───────────────────────────────────────────────────────────────────────────────┘
```

## Six commands

```
  brief ingest      every source any enabled profile references     daily
                    --since <date> reaches back, for sources that can
  brief synthesize  the week's candidates → one brief per profile   Monday
                    refuses a past week; that is backfill's job
  brief backfill    past weeks that have no brief, by PUBLICATION   on demand
                    date, oldest first, skipping what is done
  brief deliver     archive to disk, then send; once                Monday
  brief reindex     rebuild derived fields from stored raw records  on demand
                    offline: 1,310 rows in 0.24s, no network
  brief doctor      is each profile's provider reachable, and why not
```

The first four are idempotent. A rerun after a failure never double-counts or double-sends.

## Three zones, and the boundary between them

The unusual thing about this codebase is that it is deliberately three kinds of code, and
the rules differ per zone. `CLAUDE.md` states them as non-negotiables.

```
  ┌─ src/brief/lib/ ──────────── 14 modules, 3,883 lines, 672 tests ──────────┐
  │                                                                            │
  │  SEEDS. Written here, shaped to graduate into ~/AI/codeLibrary.            │
  │  Nothing here may import from brief/. No Item, no config, no db handle.    │
  │  Explicit values in, plain dicts out. The docstring IS the design doc.     │
  │  Approved boundaries live in docs/seeds/<slug>.md.                         │
  │                                                                            │
  │   feed_fetch  page_main_text  nsf_award_search  nih_reporter_search        │
  │   usaspending_award_search    pubmed_search     keyword_relevance          │
  │   context_packer  llm_json_contract  cited_digest_render  markdown_email   │
  │   config_env_interpolate      macos_keychain_read   period_backfill_plan   │
  │                                                                            │
  │   13 of 14 band `strong` on the library's harvest scorer.                  │
  └────────────────────────────────────────────────────────────────────────────┘
                                     ▲
                                     │ called by, never the reverse
                                     │
  ┌─ src/brief/ ───────────────── 8 modules, 2,615 lines ─────────────────────┐
  │                                                                            │
  │  APP GLUE. Knows the column names, the profile field names, the ordering.  │
  │  Imports brief.models on purpose — that layer is what keeps seeds clean.   │
  │                                                                            │
  │   cli  db  profile  select  synthesize  render  deliver  models            │
  │   sources/ — 6 adapters, 496 lines, 20-40 each, thin by rule               │
  └────────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
  ┌─ src/brief/vendor/ ────────── 12 modules, 2,360 lines ────────────────────┐
  │                                                                            │
  │  NOT OURS. Copied from codeLibrary, provenance in .lib/vendored.json.      │
  │  Never hand-edited. A needed change goes upstream and comes back.          │
  │                                                                            │
  │   cli-llm-providers   ollama-local-llm    sqlite-versioned-schema          │
  │   layered-config-overlay   secret-redaction   secret-scanner               │
  │                                                                            │
  │   All `verified` tier. ollama-local-llm carries a change contributed       │
  │   FROM this project: the options/keep_alive passthrough.                   │
  └────────────────────────────────────────────────────────────────────────────┘
```

## The rule that shapes everything

The model is never trusted. It is asked for JSON; **code** enforces the contract, on
four rungs, and the caller takes the highest one its provider supports.

```
   the profile's bucket names ──> enum in the schema ──┐
                                                        │  same object, three jobs:
                                                        ├─ rendered into the prompt
                                                        ├─ the decoder's grammar
                                                        └─ the validator
                                                        │
   RUNG 1  CONSTRAIN  ollama only ◄─────────────────────┘
         │  format = schema. Enforced BY THE DECODER, measured:
         │  required · additionalProperties · enum · minItems
         │  NOT uniqueItems — which is why rung 3 still runs
         ▼
   model returns text
         │
         ▼
   RUNG 2  extract first JSON object ─── fails ──> retry ONCE with the error
         │                                              │
         ▼                                              └─ fails again ──> STUB PAGE
   RUNG 3  validate against prompts/schema.json                            (never silence)
         │           a grammar constrains SHAPE, never TRUTH:
         │           told to violate its schema, the model obeyed the
         │           grammar and padded a section with junk
         │
         ▼
   translate prompt positions → database ids
         │
         ▼
   DROP any entry citing an id that was not in the input
         │                          │
         │                          └──> counted in the footer
         ▼
   neutralise link syntax in the model's own text
         │
         ▼
   every link on the page goes to a cited source
```

That chain is why a brief can be forwarded without the sender vouching for it personally.

## Data flow, with the real numbers from one week

```
  1,360 items in the database
      │  window: fetched in the last 7 days AND published within 90
      ▼
    177 considered
      │  keyword filter: any_of, minus none_of, ranked by distinct hits
      ▼
     59 matched
      │  packed into the provider's context budget, each item capped
      ▼
     21 sent at a 32k window  ·  59 sent at 65k (the shipped setting)
      │  one model call
      ▼
     14 entries, every one citing a source, 0 dropped
```

## Schema

```
  ┌──────────────────────────────┐        ┌──────────────────────────────┐
  │ items                        │        │ briefs                       │
  ├──────────────────────────────┤        ├──────────────────────────────┤
  │ id            INTEGER PK     │        │ id            INTEGER PK     │
  │ source        TEXT           │        │ profile       TEXT       [U] │
  │ source_key    TEXT       [U] │        │ week_start    TEXT       [U] │
  │ external_id   TEXT           │        │ generated_at  TEXT           │
  │ url           TEXT           │        │ provider      TEXT           │
  │ title         TEXT           │        │ model         TEXT           │
  │ body          TEXT           │        │ prompt_hash   TEXT           │
  │ published_at  TEXT           │        │ input_item_ids TEXT          │
  │ fetched_at    TEXT           │        │ raw_response  TEXT           │
  │ content_hash  TEXT       [U] │        │ result_json   TEXT           │
  │ raw_json      TEXT           │        │ markdown      TEXT           │
  │ facts_json    TEXT           │        │ sent_at       TEXT           │
  └──────────────────────────────┘        └──────────────────────────────┘

  ┌──────────────────────────────┐
  │ source_state                 │   cache validators, so an unchanged
  ├──────────────────────────────┤   feed costs one conditional request
  │ source_key    TEXT PK        │
  │ etag          TEXT           │
  │ last_modified TEXT           │
  │ updated_at    TEXT           │
  └──────────────────────────────┘

  [U] = part of a UNIQUE constraint

  content_hash = sha256(source_key + url + normalized body)
  dedup        = ON CONFLICT(source_key, content_hash) DO NOTHING
                 named, not INSERT OR IGNORE, which would swallow a NOT NULL
                 violation as "already seen"

  source_key identifies the FETCH, not the source: `nsf`, or `nsf#7f784cd7`
  when a profile supplied parameters. Two profiles querying one endpoint
  differently must not see each other's rows.

  facts_json and published_at are DERIVED from raw_json and are NOT in the
  hash — so `brief reindex` rebuilds them offline without creating duplicates.
```

## What is not here

No web UI. No framework. No scheduler yet — no Compose file, no cron. The
`anthropic-api` provider raises a message naming what does work rather than being
half-built. `changedetection.io` is configured but not running, so its source fails
every ingest and is correctly reported silent.
