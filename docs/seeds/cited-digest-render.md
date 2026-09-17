# Seed boundary: cited-digest-render

## Purpose
Render an LLM-produced, bucketed digest to Markdown in which every entry links to the input items it cites, dropping (and reporting) any entry whose citations are empty or point at an id the model was never given.

## when_to_use (draft for FEATURE.toml)
An LLM handed you a structured summary whose entries cite the ids of the inputs it saw, and the page you are about to publish must contain nothing that does not link back to a real source — with a count of what was cut, so a loosely citing model shows up in the numbers instead of silently thinning the page.

## Inputs
- result: Mapping — the parsed digest: {'buckets': [{'name': str, 'entries': [{'text': str, 'item_ids': [int, ...]}]}], 'watch_list': [{'text', 'item_ids'}], ...}; unknown top-level keys (e.g. 'merged') are ignored; a missing 'buckets' or 'watch_list' is treated as empty
- urls: Mapping[int, str] — item id -> URL for every item that was sent to the model; its key set IS the set of known ids
- title: str — page heading (already formatted by the caller, e.g. 'AI at ISU')
- subtitle: str = '' — one line under the title, e.g. 'Week of 2026-09-14'; omitted when empty
- bucket_order: Sequence[str] | None — bucket names in the order the page should show them; buckets not named are appended in the order the model returned them; buckets left empty after drops are omitted
- watch_list_heading: str = 'Watch list'
- footer: str = '' — verbatim trailing paragraph (provenance line, provider name, 'n of m candidates'); the caller composes it, the seed only places it
- link_label: Callable[[int], str] = str — text shown for each citation link, given the item id

## Outputs
- RenderResult dataclass: markdown: str (the page; '' only when nothing survived), kept: int (entries rendered, buckets + watch list), dropped: list[DroppedEntry]
- DroppedEntry dataclass: section: str (bucket name or the watch-list heading), text: str, item_ids: list (as received), reason: Literal['no_ids', 'unknown_id'], unknown: tuple[int, ...] (the offending ids, empty for 'no_ids')
- enforce_citations returns (clean_result: dict, dropped: list[DroppedEntry]) — same shape as the input result with offending entries removed and now-empty buckets removed, so the caller can store or diff the clean structure
- Markdown shape is fixed: '# title', optional subtitle line, '## <bucket>' sections of '- text [label](url) [label](url)' bullets in bucket_order, '## Watch list' section, footer paragraph; entry text has internal newlines collapsed to single spaces so one entry is always one bullet; duplicate ids within an entry link once, in first-seen order

## Dependencies
- none

## Must NOT know about
- The app's config.yaml, Profile or the profile YAML schema — bucket order, title, footer arrive as plain values
- The Item dataclass, SQLite, the items or briefs tables — the id->url map is a plain Mapping the caller builds
- prompts/schema.json or jsonschema — the seed assumes the shape and raises TypeError/ValueError on a malformed entry instead of validating
- The LLMProvider, provider name, or retry logic — the footer is a string the caller already composed
- Logging setup — drops are returned, never logged; the caller decides what a drop count means
- Markdown-to-HTML conversion, SMTP, file paths under briefs/ — the seed returns a string
- Type coercion of ids — '12' is not 12; if a model returns string ids that is a schema failure upstream, not a rendering fallback

## Public API
```python
@dataclass(frozen=True)
class DroppedEntry:
    section: str
    text: str
    item_ids: list
    reason: Literal['no_ids', 'unknown_id']
    unknown: tuple[int, ...] = ()
@dataclass(frozen=True)
class RenderResult:
    markdown: str
    kept: int
    dropped: list[DroppedEntry]
def enforce_citations(result: Mapping[str, Any], known_ids: Collection[int], *, watch_list_heading: str = 'Watch list') -> tuple[dict, list[DroppedEntry]]:
    """Copy of result with uncited or mis-cited entries removed and empty buckets dropped, plus the drops."""
def render_markdown(result: Mapping[str, Any], urls: Mapping[int, str], *, title: str, subtitle: str = '', bucket_order: Sequence[str] | None = None, watch_list_heading: str = 'Watch list', footer: str = '', link_label: Callable[[int], str] = str) -> str:
    """Markdown for an already-clean result; raises KeyError on an id missing from urls."""
def render_digest(result: Mapping[str, Any], urls: Mapping[int, str], *, title: str, subtitle: str = '', bucket_order: Sequence[str] | None = None, watch_list_heading: str = 'Watch list', footer: str = '', link_label: Callable[[int], str] = str) -> RenderResult:
    """enforce_citations then render_markdown; the one call the app makes."""
```

## Test harness
none

## Approved module docstring (write this verbatim into the module)
```
Render a bucketed, citation-carrying digest to Markdown, keeping only entries that link to a real source.

An LLM was given a numbered list of items and asked for JSON: named buckets of entries, a watch
list, and on every entry an ``item_ids`` list naming the inputs it drew on. This module turns that
JSON into the page a reader sees, and it is where the citation rule is enforced by code rather than
by the prompt. An entry with no ``item_ids``, or with any id absent from the caller's id->url map,
is dropped and reported; it never reaches the page. Dropping the whole entry on one bad id is
deliberate: a claim that cites one real source and one invented one cannot be trusted either.

Contract:
- ``urls`` (id -> URL) is the whole set of known ids. Ids are compared as given; no coercion.
- Buckets come out in ``bucket_order`` first, then any others in model order; a bucket left empty
  after drops is omitted. Entry text is emitted verbatim except that newlines collapse to spaces.
- ``enforce_citations`` and ``render_markdown`` are usable alone; ``render_digest`` composes them
  and returns the page plus every drop with its reason, so the caller can count, log, or refuse
  to send when too much was cut.

Deliberately not done here: JSON extraction and schema validation (upstream), Markdown-to-HTML
and delivery (downstream), logging, and any knowledge of where ids or URLs come from.

No runtime dependencies: dataclasses and typing only. Markdown is emitted as text; a Markdown
library would add nothing but a way for formatting to drift.
```

## Prior art
verdict: seed

Five phrasings, none matched. No feature in the library renders a structured result to Markdown, and none enforces citations on an LLM answer in code: rag-chat-service requests '(page N)' citations in the prompt only. The nearest part, secret-scanner's render_report, shares only the design stance (rendering kept separate from computation, report the true count even when output is truncated), which this seed adopts as a principle but cannot copy as code. Plant a new seed.

## Critic verdict: keep
Filter and render are one situation (a page that must not contain uncited claims); splitting would make the renderer trust its input. Both halves stay public. Pure, no deps.

### Boundary fixes to apply at write time
- 'watch_list' is the app schema's top-level key baked into the seed; generalize to extra_sections: Mapping[str, str] = {'watch_list': 'Watch list'} (result key -> heading) so a second app with 'open_questions' needs no fork
- link_label default str renders '[12](url)' — legible but ugly; keep default but note the caller can pass an ordinal or domain label; no change to boundary
- enforce_citations takes known_ids: Collection[int] while render_markdown takes urls: Mapping — good; make render_digest derive known_ids from urls.keys() and say so
- ids compared as given (no '12' -> 12 coercion) is right; the schema.json must enforce integer item_ids so a string id fails validation upstream, not silently drops here — cross-reference in the docstring
