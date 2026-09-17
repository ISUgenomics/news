"""End-to-end: rows in, brief out, with the safety rules exercised.

These are the acceptance criteria from BUILD-SPEC.md. They drive the real
modules against a stub provider — a plain callable, because that is all the
`LLMProvider` surface needs — so nothing here touches a network or a CLI.
"""

from __future__ import annotations

import json
import re
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
        max_words=1500,
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


def test_more_keyword_hits_outranks_being_newer(conn):
    """The documented policy: hit count first, recency only as the tiebreak."""
    db.upsert_items(
        conn,
        [
            Item(
                source="feed_a",
                url="https://example.test/broad",
                title="AI and machine learning centre wins award",
                body="Work on artificial intelligence and machine learning.",
                published_at="2026-09-11T00:00:00Z",
            )
        ],
        now=NOW,
        source_key="feed_a",
    )
    selection, _, _ = run(conn, make_profile(), StubProvider(good_reply()))
    first = selection.rendered[0][1]
    assert "centre wins award" in first, (
        "three distinct keyword hits must outrank two items that are five days newer"
    )
    assert selection.candidates[0].hits > selection.candidates[1].hits


def test_item_text_is_redacted_before_it_can_reach_the_model(conn):
    """Scraped pages are text we did not write, and the prompt is stored."""
    db.upsert_items(
        conn,
        [
            Item(
                source="feed_a",
                url="https://example.test/leak",
                title="An AI award announcement",
                body="Contact the PI. Their key is sk-ant-api03-" + "A" * 40 + " sorry.",
                published_at="2026-09-16T00:00:00Z",
            )
        ],
        now=NOW,
        source_key="feed_a",
    )
    provider = StubProvider(good_reply())
    run(conn, make_profile(), provider)

    sent = " ".join(m["content"] for m in provider.calls[0])
    assert "sk-ant-api03-AAAA" not in sent, "a pasted key must not reach the model"
    assert "An AI award announcement" in sent, "and the item is still sent"


def test_a_small_context_window_leaves_items_out_and_says_so(conn):
    profile = make_profile()
    provider = StubProvider(good_reply(), window=1500)
    selection, _, page = run(conn, profile, provider)
    assert selection.left_out, "a tiny window must not silently send everything"
    assert "did not fit the context window" in page.markdown


def test_items_are_numbered_from_one_and_map_back_to_row_ids(conn):
    """The mapping must follow the SEND order, not the row order.

    Asserting only that the keys are 1..n is tautological — that is how they
    are built. What matters is that position N in the prompt resolves to the
    database id of the item actually rendered at position N, because the
    renderer turns the model's citations into links through this map. Get it
    wrong and every link points at the wrong article while everything still
    looks well-formed.
    """
    profile = make_profile()
    selection, _, _ = run(conn, profile, StubProvider(good_reply()))
    mapping = selection.citation_map()

    assert sorted(mapping) == list(range(1, selection.sent + 1))
    for position, (item_id, text) in enumerate(selection.rendered, start=1):
        assert mapping[position] == item_id

    # And the order is the ranked one. Both fixture items hit exactly one
    # keyword, so the documented tiebreak applies: newer first. The hire is
    # dated 09-16 and the award 09-15, so the hire leads despite the lower id.
    titles = [t for _i, t in selection.rendered]
    assert "faculty hire" in titles[0]
    assert "machine learning award" in titles[1]
    assert mapping[1] > mapping[2], "ranked order, not ascending row id"


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


# --- found by the adversarial review; each of these was a real defect --------


def test_the_provenance_line_comes_from_config_not_from_code(conn):
    """Rule 2: no string in src/brief/ may name an institution."""
    profile = make_profile(
        config={
            "smtp": {},
            "delivery": {"provenance": "Prepared by the Example Office of Research."},
        }
    )
    _, _, page = run(conn, profile, StubProvider(good_reply()))
    assert "Example Office of Research" in page.markdown

    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "brief"
    for path in src.rglob("*.py"):
        if "vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for named in ("Iowa State", "iastate", "VPR", "bioinformatics facility"):
            assert named not in text, f"{path.name} names {named!r}; that belongs in config"


def test_a_default_provenance_still_promises_only_what_the_code_delivers(conn):
    _, _, page = run(conn, make_profile(), StubProvider(good_reply()))
    assert "links to its source" in page.markdown, "the citation rule is the promise"


def test_a_section_the_model_invented_is_dropped_and_counted(conn):
    """The profile decides what sections a brief has, not the model."""
    reply = json.dumps(
        {
            "buckets": [
                {"name": "Funding", "entries": [{"text": "A real one.", "item_ids": [1]}]},
                {"name": "Rumours", "entries": [{"text": "Made up.", "item_ids": [1]}]},
            ]
        }
    )
    _, _, page = run(conn, make_profile(), StubProvider(reply))
    assert "Rumours" not in page.markdown
    assert "Made up" not in page.markdown
    assert "A real one" in page.markdown
    assert "not declared by this profile" in page.markdown, "and the reader is told"


def test_prompt_hash_changes_when_the_item_rendering_changes(conn):
    """Otherwise two briefs over genuinely different input share a hash."""
    prompt = synth_mod.render_system_prompt(make_profile(), schema=synth_mod.load_schema())
    assert synth_mod.prompt_hash(prompt, item_render_version=1) != synth_mod.prompt_hash(
        prompt, item_render_version=2
    )


def test_a_link_the_model_wrote_does_not_survive_into_the_page(conn):
    """Through render(), not just the helper.

    Testing neutralize_links directly passes even if render() never calls it,
    which is how this was nearly missed. The page's contract is that every link
    goes to a cited source, so the check has to be on the rendered artifact.
    """
    reply = json.dumps(
        {
            "buckets": [
                {
                    "name": "Funding",
                    "entries": [
                        {
                            "text": "Read [the announcement](https://evil.test) or <https://also-evil.test>.",
                            "item_ids": [1],
                        }
                    ],
                }
            ]
        }
    )
    _, _, page = run(conn, make_profile(), StubProvider(reply))

    assert r"\[the announcement\]" in page.markdown, "the brackets are escaped, not a link"
    assert "<https://also-evil.test>" not in page.markdown, "the autolink brackets are gone"
    assert "the announcement" in page.markdown, "but the words survive"

    from brief.lib.markdown_email import markdown_to_html

    html = markdown_to_html(page.markdown)
    hrefs = re.findall(r'href="([^"]+)"', html)
    assert hrefs, "the citation link is still there"
    assert all("evil.test" not in h for h in hrefs), f"only cited sources may be links: {hrefs}"


def test_the_item_cap_applies_to_the_rendered_item_not_just_the_body(conn):
    """Why a cap equal to the storage cap still truncated the longest items.

    `select` renders each item as a header (source, date, title, url) plus the
    body, and the cap applies to that whole string. Setting max_item_chars to
    the storage cap therefore still clips the longest items by the length of the
    header, which is exactly what a week of real data showed.
    """
    body = "x" * 500
    db.upsert_items(
        conn,
        [
            Item(
                source="feed_a",
                url="https://example.test/long",
                title="A long AI award announcement",
                body=body,
                published_at="2026-09-16T00:00:00Z",
            )
        ],
        now=NOW,
        source_key="feed_a",
    )
    profile = make_profile()

    at_body_length = select_mod.select(
        [r for r in db.items_fetched_since(conn, ["feed_a"], since=NOW - timedelta(days=7))
         if "long" in r["url"]],
        profile,
        context_window_tokens=100_000,
        settings={"max_item_chars": len(body)},
    )
    assert at_body_length.truncated, "the header pushes the rendered item over the cap"

    with_header_room = select_mod.select(
        [r for r in db.items_fetched_since(conn, ["feed_a"], since=NOW - timedelta(days=7))
         if "long" in r["url"]],
        profile,
        context_window_tokens=100_000,
        settings={"max_item_chars": len(body) + 1024},
    )
    assert not with_header_room.truncated


def test_the_two_caps_are_never_equal():
    """Equality is the specific trap, and which one binds is a real choice.

    `max_item_chars` caps the rendered item — body plus a header of source,
    date, title and URL — while `body_cap_bytes` caps the body alone. Set them
    equal and every maximum-length body is silently clipped by the length of
    that header, which is what a real week showed.

    Either ordering is legitimate. Larger storage cap means the prompt budget
    binds, which is a deliberate trade against the model's context window.
    Larger item cap means storage binds. Equal means an accident.
    """
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parent.parent
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    body_cap = cfg["ingest"]["body_cap_bytes"]
    item_cap = cfg["select"]["max_item_chars"]
    assert item_cap != body_cap, (
        f"both caps are {item_cap}; the per-item header then clips every "
        f"maximum-length body by its own length"
    )


def test_the_named_values_a_brief_must_quote_reach_the_model(conn):
    """The profile asks for 'sponsor, PI, and amount verbatim'.

    The seeds extract all three; the app dropped them for a while, so the model
    was asked for a PI it had never been shown and duly wrote 'the item does not
    name a specific PI'. These are rendered as labelled lines rather than left
    in prose, because they are values to repeat rather than text to read.
    """
    conn.execute(
        "UPDATE items SET facts_json = ? WHERE id = 1",
        ('{"PI": "Hongwei Zhang", "Amount": "1567090", "Sponsor": "NSF"}',),
    )
    conn.commit()

    provider = StubProvider(good_reply())
    run(conn, make_profile(), provider)
    sent = " ".join(m["content"] for m in provider.calls[0])

    assert "PI: Hongwei Zhang" in sent
    assert "Amount: 1567090" in sent
    assert "Sponsor: NSF" in sent


def test_an_item_with_no_facts_renders_without_that_section(conn):
    """Rows stored before the column existed must still work."""
    provider = StubProvider(good_reply())
    run(conn, make_profile(), provider)
    sent = " ".join(m["content"] for m in provider.calls[0])
    assert "PI:" not in sent
    assert "University wins machine learning award" in sent


def test_malformed_stored_facts_are_ignored_rather_than_crashing(conn):
    conn.execute("UPDATE items SET facts_json = ? WHERE id = 1", ("not json",))
    conn.commit()
    provider = StubProvider(good_reply())
    run(conn, make_profile(), provider)
    assert provider.calls, "a bad facts blob must not stop the week"


def test_the_item_render_version_tracks_the_item_format(conn):
    """It feeds prompt_hash, so two briefs with one hash saw the same input."""
    from brief.select import ITEM_RENDER_VERSION

    assert ITEM_RENDER_VERSION >= 2, (
        "adding the facts lines changed what the model sees; the version must move "
        "or a regenerated brief would look comparable to one that saw less"
    )


def test_the_length_rule_is_a_ceiling_from_the_profile_not_a_hardcoded_target(conn):
    """Coverage over brevity: a busy week should be a longer brief, not a
    rationed one. The owner would rather hear about an award than have it
    dropped for length, so the number is a profile ceiling and the rule says so."""
    from brief.models import DEFAULT_MAX_WORDS

    schema = synth_mod.load_schema()
    default = synth_mod.render_system_prompt(make_profile(), schema=schema)
    assert f"{DEFAULT_MAX_WORDS} words" in default
    assert "ceiling, not a target" in default
    assert "shorten the entries rather than dropping items" in default

    generous = synth_mod.render_system_prompt(make_profile(max_words=4000), schema=schema)
    assert "4000 words" in generous
    assert "600 words" not in generous, "the old hardcoded target must be gone"


def test_no_placeholder_survives_into_the_prompt(conn):
    """A literal {max_words} reaching the model is worse than any number."""
    import re

    prompt = synth_mod.render_system_prompt(make_profile(), schema=synth_mod.load_schema())
    body = prompt.split("Return an object matching this schema")[0]
    assert not re.search(r"\{[a-z_]+\}", body), f"unsubstituted placeholder in {body[-200:]}"
