# Seed boundary: context-packer

## Purpose
Pack already-ranked text items, each capped, into a character budget and report which were left out.

## when_to_use (draft for FEATURE.toml)
You have a ranked list of text snippets and a model whose context window you only know as a number, and you need to send as many as fit while telling the reader how many did not.

## Inputs
- items: Sequence[tuple[K, str]] — (opaque key, rendered text) already in rank order; key is whatever the caller uses to cite the item (an int row id in this app)
- budget_chars: int — the total character budget for the item block
- max_item_chars: int | None — per-item cap applied before fitting (6000 in this app)
- per_item_overhead_chars: int — chars the caller will add per item for numbering/separators, counted against the budget
- max_items: int | None — upper bound on count (profile max_items)
- truncation_marker: str — appended to a capped text so the model sees it was cut
- context_window_tokens, chars_per_token, prompt_overhead_chars, output_reserve_tokens — ints for context_budget_chars()

## Outputs
- PackResult(packed: list[tuple[K, str]], left_out: list[K], chars_used: int, budget_chars: int, truncated: list[K]) — packed is a strict prefix of the input in the same order; left_out is the remaining keys in order; truncated lists keys whose text was capped
- context_budget_chars(...) -> int — never negative; 0 when overhead and reserve exhaust the window

## Dependencies
- none

## Must NOT know about
- the Item or Profile dataclasses (takes (key, text) pairs)
- config.yaml / profile YAML / the profile schema (max_items and cap arrive as ints)
- the LLMProvider Protocol or any provider object (context_window() is called by the caller, the int is passed in)
- SQLite, items.db, row shapes
- logging setup (returns counts; the caller logs)
- how an item is rendered into the prompt (the caller renders title/url/body to one string first)
- secret redaction (caller redacts before packing)
- hardcoded paths, prompts/system.md, the previous-week result (that is just prompt_overhead_chars)

## Public API
```python
def context_budget_chars(context_window_tokens: int, *, chars_per_token: int = 4, prompt_overhead_chars: int = 0, output_reserve_tokens: int = 4096) -> int
@dataclass(frozen=True)
class PackResult(Generic[K]):
    packed: list[tuple[K, str]]
    left_out: list[K]
    truncated: list[K]
    chars_used: int
    budget_chars: int
def cap_text(text: str, max_chars: int, marker: str = "…") -> tuple[str, bool]
def pack_items(items: Sequence[tuple[K, str]], budget_chars: int, *, max_item_chars: int | None = None, per_item_overhead_chars: int = 0, max_items: int | None = None, truncation_marker: str = "…") -> PackResult[K]
```

## Test harness
none

## Approved module docstring (write this verbatim into the module)
```
Pack ranked text items into a character budget, capping each, and say what was left out.

The situation: one LLM call has to carry as many candidate items as fit, there is no
tokenizer (CLI and Ollama providers expose only a context-window hint), and the
caller must be able to tell the reader "n of m candidates" honestly. This module is
the arithmetic and the loop, nothing else.

Contract:
- ``context_budget_chars(window_tokens, chars_per_token=4, prompt_overhead_chars=0,
  output_reserve_tokens=4096)`` -> chars available for the item block, floored at 0.
- ``pack_items(items, budget_chars, *, max_item_chars=None, per_item_overhead_chars=0,
  max_items=None, truncation_marker="…")`` -> ``PackResult``. ``items`` are
  ``(key, text)`` pairs already in rank order; keys are opaque and returned as given.
- Fitting is first-fit-then-stop: the packed list is a strict prefix of the input, so
  rank order is honored and ``left_out`` is everything after the first item that did
  not fit. It does not skip ahead to squeeze in a smaller lower-ranked item; that
  would silently reorder by size, not rank.
- Capping happens before fitting; a capped text ends with ``truncation_marker`` and
  its key is listed in ``truncated``. An item that exceeds the whole budget even after
  capping is left out, not partially packed.
- Pure and deterministic. Same inputs, same result, no I/O, no logging.

Deliberately not here: ranking or relevance filtering (the caller decides order),
rendering an item to prompt text (the caller passes the final string), token
counting (chars_per_token is a stated assumption, not a measurement), and any
notion of provider, profile, or database. Zero dependencies: stdlib dataclasses only.
```

## Prior art
verdict: seed

No whole feature packs ranked items into a budget or reports leftovers. The only real part hit is estimate_tokens() in hybrid-rag-retrieval, a one-line character/4 estimate; it is too small to copy as a part and goes the other direction (corpus -> tokens, not window -> chars). The seed adopts its convention (chars_per_token default 4) so the two features agree, and stays a seed. hybrid-rag-retrieval was already considered and rejected by the spec for this pipeline (nothing is retrieved; the week's items are the whole input).

## Critic verdict: keep
One job: fit a ranked list into a limit and report the remainder. Budget arithmetic is a pure helper in the same module, not a second seed. Nothing app-shaped in the inputs.

### Boundary fixes to apply at write time
- chars_per_token default 4 matches hybrid-rag-retrieval's DEFAULT_CHARS_PER_TOKEN; say so in the docstring so the two features agree deliberately
- truncation_marker default '…' (non-ASCII) is fine but document it counts toward max_item_chars
- PackResult.packed carries the capped text; make explicit that callers citing by key must not re-render from the original (or return both) — otherwise synthesize.py silently sends uncapped bodies
