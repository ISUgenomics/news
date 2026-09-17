"""End-to-end: rows in, brief out, with the safety rules exercised.

These are the acceptance criteria from BUILD-SPEC.md. They drive the real
modules against a stub provider — a plain callable, because that is all the
`LLMProvider` surface needs — so nothing here touches a network or a CLI.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from brief import (
    db,
    render as render_mod,
    select as select_mod,
    synthesize as synth_mod,
)
from brief.deliver import AlreadySent, deliver
from brief.lib.llm_json_contract import JsonContractError
from brief.models import Bucket, Delivery, Item, Profile, Relevance, SourceRef

NOW = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
WEEK = "2026-09-14"


class StubProvider:
    """A provider is anything with these five members. Records what it saw."""

    name = "stub"

    def __init__(self, *replies: str, window: int = 100_000):
        self.replies = list(replies)
        self.window = window
        self.calls: list[list[dict]] = []

    def available(self) -> bool:
        return True

    def status(self) -> str:
        return "stub ready"

    def context_window(self) -> int:
        return self.window

    def complete(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]


def make_profile(**over) -> Profile:
    base = dict(
        name="test",
        title="Test Brief",
        audience="a test reader",
        persona="analyst",
        sources=(SourceRef("feed_a"),),
        relevance=Relevance(
            any_of=("machine learning", "AI"), none_of=("bake sale",), max_items=80
        ),
        buckets=(Bucket("Funding", "awards"), Bucket("People", "hires")),
        delivery=Delivery(sender="from@example.test", to=("to@example.test",)),
        llm={"provider": "stub"},
        config={"smtp": {"host": "localhost", "port": 2525}},
    )
    base.update(over)
    return Profile(**base)


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "items.db")
    db.upsert_items(
        c,
        [
            Item(
                source="feed_a",
                url="https://example.test/award",
                title="University wins machine learning award",
                body="The National Science Foundation awarded $1,234,567 to Dr. Pat Lee.",
                published_at="2026-09-15T00:00:00Z",
            ),
            Item(
                source="feed_a",
                url="https://example.test/hire",
                title="New AI faculty hire announced",
                body="Dr. Sam Rivera joins the department.",
                published_at="2026-09-16T00:00:00Z",
            ),
            Item(
                source="feed_a",
                url="https://example.test/excluded",
                # Matches any_of AND none_of, so only the exclusion list can
                # drop it. Without this, none_of would be untested: an item
                # that fails any_of is dropped whether none_of works or not.
                title="AI club bake sale raises funds",
                body="Cookies were sold at the machine learning club table.",
                published_at="2026-09-16T00:00:00Z",
            ),
            Item(
                source="feed_a",
                url="https://example.test/offtopic",
                title="Parking lot resurfacing begins",
                body="Lot 4 closes Monday.",
                published_at="2026-09-16T00:00:00Z",
            ),
        ],
        now=NOW,
    )
    yield c
    c.close()


def good_reply(ids=(1,)) -> str:
    return json.dumps(
        {
            "buckets": [
                {
                    "name": "Funding",
                    "entries": [
                        {"text": "NSF awarded $1,234,567.", "item_ids": list(ids)}
                    ],
                }
            ],
            "watch_list": [],
        }
    )


def run(conn, profile, provider, *, week=WEEK):
    rows = db.items_fetched_since(
        conn, [r.name for r in profile.sources], since=NOW - timedelta(days=7)
    )
    selection = select_mod.select(
        rows, profile, context_window_tokens=provider.context_window()
    )
    result = synth_mod.synthesize(profile, selection, provider)
    urls = db.urls_for_ids(conn, [i for i, _ in selection.rendered])
    page = render_mod.render(
        result.result,
        profile,
        selection,
        week_start=week,
        provider_name=result.provider_name,
        item_urls=urls,
    )
    return selection, result, page


# --- selection ------------------------------------------------------------


def test_keyword_filter_keeps_relevant_and_drops_the_rest(conn):
    profile = make_profile()
    selection, _, _ = run(conn, profile, StubProvider(good_reply()))
    assert selection.considered == 4
    assert selection.matched == 2
    text = " ".join(t for _, t in selection.rendered)
    assert "bake sale" not in text, "none_of must drop an item that any_of matched"
    assert "Parking" not in text, "any_of must drop an item that matches nothing"


def test_a_small_context_window_leaves_items_out_and_says_so(conn):
    profile = make_profile()
    provider = StubProvider(good_reply(), window=1500)
    selection, _, page = run(conn, profile, provider)
    assert selection.left_out, "a tiny window must not silently send everything"
    assert "did not fit the context window" in page.markdown


def test_items_are_numbered_from_one_and_map_back_to_row_ids(conn):
    profile = make_profile()
    selection, _, _ = run(conn, profile, StubProvider(good_reply()))
    mapping = selection.citation_map()
    assert sorted(mapping) == list(range(1, selection.sent + 1))
    assert all(isinstance(v, int) for v in mapping.values())


# --- the JSON contract ----------------------------------------------------


def test_a_valid_reply_is_accepted_on_the_first_attempt(conn):
    provider = StubProvider(good_reply())
    _, result, _ = run(conn, make_profile(), provider)
    assert result.attempts == 1
    assert len(provider.calls) == 1


def test_broken_json_triggers_exactly_one_retry(conn):
    provider = StubProvider("not json at all", good_reply())
    _, result, _ = run(conn, make_profile(), provider)
    assert result.attempts == 2
    assert len(provider.calls) == 2, "one retry, not a loop"


def test_the_retry_turn_shows_the_model_what_it_wrote(conn):
    provider = StubProvider("not json at all", good_reply())
    run(conn, make_profile(), provider)
    retry = provider.calls[1]
    joined = " ".join(m["content"] for m in retry)
    assert "not json at all" in joined, "the failed reply must be in the retry turn"


def test_two_bad_replies_raise_rather_than_guess(conn):
    provider = StubProvider("garbage", "still garbage")
    with pytest.raises(JsonContractError):
        run(conn, make_profile(), provider)


def test_a_reply_violating_the_schema_is_rejected(conn):
    bad = json.dumps(
        {"buckets": [{"name": "Funding", "entries": [{"text": "no citation"}]}]}
    )
    provider = StubProvider(bad, good_reply())
    _, result, _ = run(conn, make_profile(), provider)
    assert result.attempts == 2, "an entry with no item_ids must fail validation"


def test_a_json_object_wrapped_in_prose_is_extracted(conn):
    provider = StubProvider(
        f"Here you go:\n```json\n{good_reply()}\n```\nHope that helps."
    )
    _, result, _ = run(conn, make_profile(), provider)
    assert result.attempts == 1
    assert result.result["buckets"][0]["name"] == "Funding"


# --- the citation rule ----------------------------------------------------


def test_an_entry_citing_an_unknown_item_is_dropped(conn):
    provider = StubProvider(good_reply(ids=(999,)))
    _, _, page = run(conn, make_profile(), provider)
    assert page.kept == 0
    assert page.dropped_count == 1
    assert "1,234,567" not in page.markdown, (
        "an uncited claim must not reach the reader"
    )


def test_a_good_entry_survives_and_links_to_its_source(conn):
    provider = StubProvider(good_reply(ids=(1,)))
    _, _, page = run(conn, make_profile(), provider)
    assert page.kept == 1
    assert page.dropped_count == 0
    assert "https://example.test/" in page.markdown


def test_the_footer_reports_dropped_entries(conn):
    provider = StubProvider(good_reply(ids=(999,)))
    _, _, page = run(conn, make_profile(), provider)
    assert "dropped for missing or unknown citations" in page.markdown


def test_the_footer_names_the_provider_and_the_provenance(conn):
    _, _, page = run(conn, make_profile(), StubProvider(good_reply()))
    assert "stub" in page.markdown
    assert "public sources" in page.markdown


def test_buckets_render_in_the_profile_order_not_the_model_order(conn):
    reply = json.dumps(
        {
            "buckets": [
                {"name": "People", "entries": [{"text": "A hire.", "item_ids": [2]}]},
                {
                    "name": "Funding",
                    "entries": [{"text": "An award.", "item_ids": [1]}],
                },
            ]
        }
    )
    _, _, page = run(conn, make_profile(), StubProvider(reply))
    assert page.markdown.index("Funding") < page.markdown.index("People")


# --- prompt provenance ----------------------------------------------------


def test_prompt_hash_changes_when_the_profile_changes(conn):
    schema = synth_mod.load_schema()
    one = synth_mod.render_system_prompt(make_profile(), schema=schema)
    two = synth_mod.render_system_prompt(
        make_profile(buckets=(Bucket("Funding", "awards"),)), schema=schema
    )
    assert synth_mod.prompt_hash(one) != synth_mod.prompt_hash(two)


def test_the_rendered_prompt_carries_the_profile_not_a_hardcoded_topic(conn):
    prompt = synth_mod.render_system_prompt(
        make_profile(audience="one PI and their lab", persona="research assistant"),
        schema=synth_mod.load_schema(),
    )
    assert "one PI and their lab" in prompt
    assert "research assistant" in prompt
    assert "Iowa State" not in prompt


# --- delivery -------------------------------------------------------------


def test_deliver_writes_the_archive_then_sends(conn, tmp_path):
    profile = make_profile()
    _, _, page = run(conn, profile, StubProvider(good_reply()))
    sent: list[dict] = []

    result = deliver(
        page.markdown,
        profile,
        week_start=WEEK,
        item_count=2,
        briefs_root=tmp_path / "briefs",
        smtp={"host": "localhost"},
        sent_at=None,
        now=NOW,
        send=lambda md, **kw: sent.append(kw) or {},
    )
    assert result.path.exists()
    assert result.path.read_text(encoding="utf-8") == page.markdown
    assert sent[0]["recipients"] == ["to@example.test"]
    assert sent[0]["subject"] == "Test Brief — week of 2026-09-14 (2 items)"


def test_delivering_an_already_sent_week_is_refused(conn, tmp_path):
    profile = make_profile()
    calls: list[int] = []
    with pytest.raises(AlreadySent):
        deliver(
            "# brief",
            profile,
            week_start=WEEK,
            item_count=1,
            briefs_root=tmp_path / "briefs",
            smtp={},
            sent_at="2026-09-14T06:30:00Z",
            now=NOW,
            send=lambda md, **kw: calls.append(1) or {},
        )
    assert calls == [], "a refused week must not reach the SMTP call at all"


def test_resend_overrides_the_refusal(conn, tmp_path):
    calls: list[int] = []
    result = deliver(
        "# brief",
        make_profile(),
        week_start=WEEK,
        item_count=1,
        briefs_root=tmp_path / "briefs",
        smtp={},
        sent_at="2026-09-14T06:30:00Z",
        resend=True,
        now=NOW,
        send=lambda md, **kw: calls.append(1) or {},
    )
    assert calls == [1]
    assert result.resent is True


def test_a_brace_in_a_title_does_not_break_the_subject(conn, tmp_path):
    profile = make_profile(title="Brief {weird}")
    from brief.deliver import subject_for

    assert (
        subject_for(profile, week_start=WEEK, item_count=3)
        == "Brief {weird} — week of 2026-09-14 (3 items)"
    )


# --- the stub page --------------------------------------------------------


def test_the_stub_page_says_why_and_how_many_candidates_there_were():
    page = render_mod.stub_markdown(
        make_profile(), week_start=WEEK, reason="claude is not on PATH.", candidates=17
    )
    assert "No brief was generated" in page
    assert "claude is not on PATH." in page
    assert "17 candidate items" in page
