# topic-brief — non-negotiables

A pipeline that turns public sources into a weekly one-page digest, once per **profile**.
`spec.md` is the product spec. `BUILD-SPEC.md` records how it was built and why.
Read `.lib/STATUS.md` if present.

## 1. Three layers, four commands

`brief ingest` (all sources, daily) · `brief synthesize --profile X | --all` ·
`brief deliver --profile X | --all` · `brief doctor` (provider status).
The first three are **idempotent**: a rerun after a failure never double-counts or double-sends.

## 2. Profiles are data

Everything topic-specific lives in `profiles/<name>.yaml`. Adding a profile is a YAML file.
**Any change that makes a new profile require code is wrong.** No string in `src/brief/`
names Iowa State, AI, a person, or a recipient.

## 3. `src/brief/lib/` is the seed zone

Each module there is written to graduate into `~/AI/codeLibrary` one day.

- **Nothing under `lib/` may import from `brief/`.** No `Item`, no config object, no db handle,
  no logger. Inputs are explicit values and callables; outputs are plain dicts and dataclasses.
- The **module docstring is the design doc** — it states the contract and what the module
  deliberately does not do. The harvest scorer reads it. Keep it current with the code.
- No top-level side effects, no environment reads at import, no hardcoded paths.
- Every seed has a sibling test in `tests/lib/test_<module>.py` and an entry in `.lib/seeds.toml`.
- Approved boundaries are in `docs/seeds/<slug>.md`. **Changing a boundary is a decision, not a
  refactor**: update that file in the same commit.

## 4. `src/brief/vendor/` is not ours

Copied from codeLibrary with `/lib-use`, provenance in `.lib/`. **Never hand-edit it.**
A needed change goes upstream (`/lib-upstream`) and comes back with `/lib-adopt`.
A defect found here is reported, not patched in place.

## 5. Adapters are thin

`src/brief/sources/*.py` map a profile's params plus a `since` datetime onto one seed call and
return `list[Item]`. They **never write to the database** and hold no HTTP or parsing logic.
20–40 lines each. If an adapter grows a second job, that job belongs in a seed.

## 6. The model is never trusted

Synthesis asks for JSON and **code enforces the contract**: extract, validate against
`prompts/schema.json`, retry once with the error. The renderer drops any entry whose `item_ids`
is empty or names an id that was not in the input, and reports the count. A brief the VPR office
forwards cannot contain an uncited claim, so that check is code, not a prompt request.
`src/brief/synthesize.py` imports the `LLMProvider` Protocol and **never a vendor SDK**.

## 7. Tests are stubs, never mocks

Real files, real sockets, real subprocesses: `tests/harness/stub_http.py`, `stub_cli.py`,
`stub_smtp.py`. Fixtures live in `tests/fixtures/`. **No test touches the live network or a real
CLI.** A saved response per source; a saved raw LLM response per provider.
After adding a check, plant the violation and watch it fail before trusting it.

## 8. Dependencies

No new dependency without a one-line justification in `pyproject.toml` and the PR.
Runtime deps today: typer, pyyaml, feedparser, trafilatura, markdown, jsonschema.
Vendored features add none. `trafilatura`, `markdown`, and `jsonschema` are **lazily imported**
inside the seed that needs them.

## 9. Prompt changes are reviewed as changes

`src/brief/prompts/system.md` and `schema.json` are the contract with the model.
A change to either gets a regenerated sample brief for every profile attached to the PR.
`briefs.prompt_hash` records which prompt produced a stored brief.

## 10. Secrets

Only from the dotenv file, referenced in config as `${VAR}` and resolved by
`lib/config_env_interpolate.py` against an env mapping passed in at the edge.
`config.yaml`, `sources.yaml`, and `profiles/` hold nothing secret. `briefs/` and
`tests/fixtures/` are generated from text we did not write — `secret-scanner` runs over both
before a commit.

## Schema, verbatim

```sql
CREATE TABLE items (
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT, url TEXT NOT NULL,
  title TEXT NOT NULL, body TEXT, published_at TEXT, fetched_at TEXT NOT NULL,
  content_hash TEXT NOT NULL, raw_json TEXT, UNIQUE(source, content_hash));

CREATE TABLE briefs (
  id INTEGER PRIMARY KEY, profile TEXT NOT NULL, week_start TEXT NOT NULL,
  generated_at TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
  prompt_hash TEXT NOT NULL, input_item_ids TEXT NOT NULL, raw_response TEXT NOT NULL,
  result_json TEXT NOT NULL, markdown TEXT NOT NULL, sent_at TEXT,
  UNIQUE(profile, week_start));
```

`content_hash` = `sha256(source + url + normalized body)`. Upsert is `INSERT OR IGNORE`.

## Item, verbatim

```python
@dataclass(frozen=True, slots=True)
class Item:
    source: str            # source name from sources.yaml
    url: str
    title: str
    body: str = ""         # capped at 8 kB by db.py, not by the adapter
    external_id: str | None = None   # award number, feed GUID, PMID
    published_at: str | None = None  # ISO 8601 from the source, if known
    raw: dict | None = None          # original record, stored as raw_json
```

`fetched_at` and `content_hash` are set by `db.py`, never by an adapter.
