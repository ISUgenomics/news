"""Tests for the five source adapters.

An adapter's whole job is the mapping: a profile's parameters and a `since`
datetime in, a `list[Item]` out, with nothing else added. So these check the
mapping and the boundary, not the HTTP — that belongs to the seeds, which have
610 tests of their own between them.

What would a bug here cost? An adapter is the one place where a seed's plain
dict becomes this app's `Item`. Drop a field and the brief loses a link, a
date, or the raw record that makes a claim checkable, and nothing else in the
pipeline would notice.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pathlib

import pytest

from brief.models import Item
from brief.sources import ADAPTERS, UnknownSourceKind, fetch
from harness.stub_http import stub_http  # noqa: F401  (pytest fixture)

SINCE = datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc)

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test</title>
<item>
  <title>A long enough summary to be kept as-is</title>
  <link>https://example.test/one</link>
  <guid>guid-one</guid>
  <pubDate>Mon, 15 Sep 2026 12:00:00 GMT</pubDate>
  <description>%s</description>
</item>
</channel></rss>""" % (b"x" * 400)


def test_every_kind_sources_yaml_names_has_an_adapter():
    """The invariant, not the inventory. This used to pin the exact set, so
    adding a source kind — the thing the design is for — failed a test about
    something else. What actually matters is that nothing in the shipped
    catalog names an adapter that does not exist: that failure surfaces at
    ingest, against the live network, on a Monday."""
    import yaml

    root = pathlib.Path(__file__).resolve().parents[1]
    catalog = yaml.safe_load((root / "sources.yaml").read_text(encoding="utf-8"))
    declared = {str(body.get("kind")) for body in catalog.values() if isinstance(body, dict)}

    missing = sorted(declared - set(ADAPTERS))
    assert not missing, f"sources.yaml names kinds with no adapter: {missing}"


def test_every_adapter_is_reachable_and_declares_its_history_support():
    from brief.sources import MODULES, supports_history

    assert set(ADAPTERS) == set(MODULES)
    for kind, module in MODULES.items():
        assert callable(module.fetch), kind
        assert isinstance(supports_history(kind), bool), kind


def test_an_unknown_kind_names_the_source_and_the_known_kinds():
    with pytest.raises(UnknownSourceKind) as caught:
        fetch("mystery", {"kind": "telepathy"}, {}, since=SINCE, now=NOW)
    assert "mystery" in str(caught.value)
    assert "rss" in str(caught.value)


def test_profile_params_override_the_source_declaration(stub_http):
    """The mechanism that lets one `nsf` source serve two profiles."""
    captured = {}

    def spy(source, params, *, since, now):
        captured.update(params)
        return []

    original = ADAPTERS["nsf"]
    ADAPTERS["nsf"] = spy
    try:
        fetch(
            "nsf",
            {"kind": "nsf", "awardee": "From sources.yaml", "timeout_s": 10},
            {"awardee": "From the profile"},
            since=SINCE,
            now=NOW,
        )
    finally:
        ADAPTERS["nsf"] = original

    assert captured["awardee"] == "From the profile", "the profile wins"
    assert captured["timeout_s"] == 10, "a key the profile did not name survives"
    assert "kind" not in captured, "kind is dispatch, not a parameter"


def test_rss_adapter_maps_a_feed_entry_onto_an_item(stub_http):
    stub_http.route("/feed", RSS)
    items, state = fetch(
        "my_feed",
        {"kind": "rss"},
        {"url": stub_http.url + "/feed"},
        since=SINCE,
        now=NOW,
    )
    assert set(state) == {"etag", "last_modified"}, (
        "a feed adapter must hand back the validators, or conditional fetch "
        "can never fire on the next run"
    )

    assert len(items) == 1
    item = items[0]
    assert isinstance(item, Item)
    assert item.source == "my_feed", (
        "the source name comes from the caller, not the feed"
    )
    assert item.url == "https://example.test/one"
    assert item.external_id == "guid-one"
    assert item.published_at.startswith("2026-09-15")
    assert len(item.body) == 400


def test_rss_adapter_falls_back_to_the_url_when_an_entry_has_no_title(stub_http):
    feed = RSS.replace(b"<title>A long enough summary to be kept as-is</title>", b"")
    stub_http.route("/feed", feed)
    items, _ = fetch(
        "f", {"kind": "rss"}, {"url": stub_http.url + "/feed"}, since=SINCE, now=NOW
    )
    assert items[0].title == "https://example.test/one", (
        "a titleless item is still linkable"
    )


def test_rss_adapter_keeps_a_thin_entry_when_its_article_page_will_not_load(stub_http):
    """Losing a real item because its page 404s would be worse than a thin one.

    The entry link must point at the stub. With the fixture's absolute
    example.test URL the expansion attempted a live DNS lookup instead, so the
    test passed on a network failure and never exercised its own 404 route.
    """
    thin = RSS.replace(b"x" * 400, b"too short").replace(
        b"https://example.test/one", (stub_http.url + "/one").encode()
    )
    stub_http.route("/feed", thin)
    hits = []
    stub_http.route("/one", lambda req: hits.append(req.path) or (404, b"gone"))

    items, _ = fetch(
        "f",
        {"kind": "rss"},
        {"url": stub_http.url + "/feed", "expand_thin_entries": True},
        since=SINCE,
        now=NOW,
    )
    assert items[0].body == "too short"
    assert hits, "the article page must actually have been requested"

    # And identically on a retry: a body that alternates between the summary
    # and the article text hashes differently, so one flaky page would store
    # the same entry twice and send both to the model as separate news.
    again, _ = fetch(
        "f",
        {"kind": "rss"},
        {"url": stub_http.url + "/feed", "expand_thin_entries": True},
        since=SINCE,
        now=NOW,
    )
    assert again[0].body == items[0].body


def test_rss_adapter_can_be_told_not_to_expand_at_all(stub_http):
    thin = RSS.replace(b"x" * 400, b"too short").replace(
        b"https://example.test/one", (stub_http.url + "/one").encode()
    )
    stub_http.route("/feed", thin)
    calls = []
    stub_http.route("/one", lambda req: calls.append(1) or b"<html><p>full</p></html>")

    fetch(
        "f",
        {"kind": "rss"},
        {"url": stub_http.url + "/feed", "expand_thin_entries": False},
        since=SINCE,
        now=NOW,
    )
    assert calls == [], "expansion off means the page is never requested"


@pytest.mark.parametrize(
    "kind,params,record",
    [
        (
            "nsf",
            {"awardee": "Some University"},
            {
                "external_id": "2412345",
                "url": "https://www.nsf.gov/awardsearch/showAward?AWD_ID=2412345",
                "title": "An award title",
                "body": "The abstract.",
                "published_at": "2026-09-01",
                "raw": {"id": "2412345"},
            },
        ),
        (
            "nih",
            {"org_names": ["SOME UNIVERSITY"]},
            {
                "external_id": "5R01GM000000-02",
                "url": "https://reporter.nih.gov/project-details/10000000",
                "title": "A project title",
                "body": "The abstract.",
                "published_at": "2026-09-02",
                "raw": {"appl_id": 10000000},
            },
        ),
        (
            "usaspending",
            {"recipient": "SOME UNIVERSITY"},
            {
                "external_id": "ASST_NON_000",
                "url": "https://www.usaspending.gov/award/ASST_NON_000",
                "title": "An award",
                "body": "The description.",
                "published_at": "2026-09-03",
                "raw": {"Award ID": "ASST_NON_000"},
            },
        ),
    ],
)
def test_award_adapters_carry_every_field_onto_the_item(
    monkeypatch, kind, params, record
):
    """Each award adapter maps the same normalized keys the same way."""
    import brief.sources.nih
    import brief.sources.nsf
    import brief.sources.usaspending

    target = {
        "nsf": (brief.sources.nsf, "search_nsf_awards"),
        "nih": (brief.sources.nih, "fetch_projects"),
        "usaspending": (brief.sources.usaspending, "search_awards"),
    }[kind]
    monkeypatch.setattr(target[0], target[1], lambda **kw: [record])

    (items, state) = fetch(kind, {"kind": kind}, params, since=SINCE, now=NOW)
    (item,) = items
    assert state == {}, "an award API has no cache validators to remember"
    assert item.source == kind
    assert item.external_id == record["external_id"]
    assert item.url == record["url"]
    assert item.title == record["title"]
    assert item.body == record["body"]
    assert item.published_at == record["published_at"]
    assert item.raw == record["raw"], (
        "the raw record is what makes a claim checkable later"
    )


@pytest.mark.parametrize("kind", ["nsf", "nih", "usaspending", "pubmed"])
def test_award_adapters_pass_the_window_as_dates_not_datetimes(monkeypatch, kind):
    """Seeds take explicit dates so their golden fixtures stay deterministic."""
    import brief.sources.nih
    import brief.sources.nsf
    import brief.sources.pubmed
    import brief.sources.usaspending

    seen = {}
    target = {
        "nsf": (brief.sources.nsf, "search_nsf_awards"),
        "nih": (brief.sources.nih, "fetch_projects"),
        "usaspending": (brief.sources.usaspending, "search_awards"),
        "pubmed": (brief.sources.pubmed, "search_pubmed"),
    }[kind]

    def spy(*a, **kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(target[0], target[1], spy)
    params = {
        "nsf": {"awardee": "X"},
        "nih": {"org_names": ["X"]},
        "usaspending": {"recipient": "X"},
        "pubmed": {"query": "x", "email": "a@example.test"},
    }[kind]
    fetch(kind, {"kind": kind}, params, since=SINCE, now=NOW)

    window = json.dumps(seen, default=str)
    assert "2026-09-10" in window.replace("/", "-"), "the since date reaches the seed"
    assert "00:00:00" not in window, (
        "a datetime would make a golden fixture time-dependent"
    )


def test_pubmed_adapter_builds_a_citation_line_as_the_body(monkeypatch):
    """A PubMed summary record has no abstract, so the citation IS the body.

    The seed reads the esummary field names out of `raw`, which is why the
    adapter must keep `raw` intact rather than flattening it. The second
    assertion pins that dependency: drop `raw` and the body silently empties.
    """
    import brief.sources.pubmed

    esummary = {
        "uid": "40000000",
        "source": "Journal of Testing",
        "authors": [{"name": "Doe A"}, {"name": "Roe J"}],
        "volume": "12",
        "pages": "1-10",
        "pubdate": "2026 Sep 1",
    }
    record = {
        "external_id": "40000000",
        "url": "https://pubmed.ncbi.nlm.nih.gov/40000000/",
        "title": "A paper title",
        "published_at": "2026-09-01",
        "raw": esummary,
    }
    monkeypatch.setattr(brief.sources.pubmed, "search_pubmed", lambda *a, **kw: [record])

    (items, _) = fetch(
        "pubmed_search",
        {"kind": "pubmed", "email": "a@example.test"},
        {"query": "testing"},
        since=SINCE,
        now=NOW,
    )
    (item,) = items
    assert "Journal of Testing" in item.body
    assert "Doe A" in item.body
    assert item.external_id == "40000000"

    without_raw = {k: v for k, v in record.items() if k != "raw"}
    monkeypatch.setattr(
        brief.sources.pubmed, "search_pubmed", lambda *a, **kw: [without_raw]
    )
    (bare_items, _) = fetch(
        "pubmed_search",
        {"kind": "pubmed", "email": "a@example.test"},
        {"query": "testing"},
        since=SINCE,
        now=NOW,
    )
    (bare,) = bare_items
    assert bare.body == "", "dropping raw silently empties the body — keep raw"


def test_no_adapter_writes_to_the_database():
    """CLAUDE.md rule 5. A grep, because the rule is about what is absent."""
    import pathlib

    for path in (
        pathlib.Path(__file__).resolve().parent.parent / "src" / "brief" / "sources"
    ).glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "import sqlite3" not in source, (
            f"{path.name} must not touch the database"
        )
        assert "from brief import db" not in source, (
            f"{path.name} must not touch the database"
        )
        assert "from brief.db" not in source, f"{path.name} must not touch the database"


# --- how a dollar figure reads ----------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (239160.0, "239160"),      # the USAspending shape: a JSON number
        ("239160", "239160"),      # the NSF shape: already a string
        (1567090, "1567090"),
        (20000000.0, "20000000"),
        (1234.56, "1234.56"),      # real cents are kept
        (0, "0"),
        (None, None),
        ("", None),
        ("n/a", "n/a"),            # unparseable passes through untouched
    ],
)
def test_money_drops_a_float_tail_without_changing_the_value(value, expected):
    """Representation, not conversion — the prompt still gets the figure verbatim.

    808 of 1041 USAspending amounts reached the model as '239160.0' and came
    back out in a brief looking like a bug.
    """
    from brief.sources import money

    assert money(value) == expected


def test_award_adapters_put_the_pi_and_amount_where_the_model_will_see_them(monkeypatch):
    """The profile asks for 'sponsor, PI, and amount verbatim'."""
    import brief.sources.nsf

    record = {
        "external_id": "1", "url": "https://x/1", "title": "t", "body": "b",
        "published_at": "2026-09-01", "raw": {}, "pi_name": "Hongwei Zhang",
        "amount": "1567090", "agency": "NSF", "program": "CISE", "awardee": "A University",
    }
    monkeypatch.setattr(brief.sources.nsf, "search_nsf_awards", lambda **kw: [record])
    (items, _) = fetch("nsf", {"kind": "nsf"}, {"awardee": "A University"}, since=SINCE, now=NOW)

    assert items[0].facts["PI"] == "Hongwei Zhang"
    assert items[0].facts["Amount"] == "1567090"
    assert items[0].facts["Sponsor"] == "NSF"


def test_usaspending_has_no_pi_because_it_names_institutions(monkeypatch):
    import brief.sources.usaspending

    record = {
        "external_id": "1", "url": "https://x/1", "title": "t", "body": "b",
        "published_at": "2026-09-01", "raw": {}, "amount": 239160.0,
        "awarding_agency": "Department of Energy", "recipient": "A University",
    }
    monkeypatch.setattr(brief.sources.usaspending, "search_awards", lambda **kw: [record])
    (items, _) = fetch("usaspending", {"kind": "usaspending"}, {"recipient": "A"}, since=SINCE, now=NOW)

    assert "PI" not in items[0].facts, "this source names the institution, not a person"
    assert items[0].facts["Amount"] == "239160", "and not 239160.0"


def test_usaspending_does_not_send_its_description_twice(monkeypatch):
    """The seed sets title = description when no title_of is given, and body IS
    that description — 1041 of 1041 rows shipped the same sentence twice. Rule 5
    puts the fix in the adapter: the recipient, agency and amount a richer title
    would carry are already in facts."""
    import brief.sources.usaspending

    record = {
        "external_id": "1", "url": "https://x/1",
        "title": "AI IN GENOMIC SELECTION", "body": "AI IN GENOMIC SELECTION",
        "published_at": "2026-09-01", "raw": {}, "amount": 1000.0,
    }
    monkeypatch.setattr(brief.sources.usaspending, "search_awards", lambda **kw: [record])
    (items, _) = fetch("usaspending", {"kind": "usaspending"}, {"recipient": "A"},
                       since=SINCE, now=NOW)
    assert items[0].title == "AI IN GENOMIC SELECTION"
    assert items[0].body == "", "the duplicate prose is dropped at the adapter"


def test_a_genuinely_different_description_is_kept(monkeypatch):
    import brief.sources.usaspending

    record = {
        "external_id": "1", "url": "https://x/1", "title": "A short label",
        "body": "A longer description that says something more.",
        "published_at": "2026-09-01", "raw": {}, "amount": 1000.0,
    }
    monkeypatch.setattr(brief.sources.usaspending, "search_awards", lambda **kw: [record])
    (items, _) = fetch("usaspending", {"kind": "usaspending"}, {"recipient": "A"},
                       since=SINCE, now=NOW)
    assert items[0].body.startswith("A longer description")


# --- the registries must not drift apart ------------------------------------


def test_every_kind_is_reachable_through_both_registries():
    """There used to be two tables — ADAPTERS and a second copy inside
    supports_history. Adding `openalex` to one left backfill silently
    skipping it. They are derived from MODULES now; this pins that."""
    from brief.sources import ADAPTERS, MODULES, supports_history

    assert set(ADAPTERS) == set(MODULES)
    for kind in MODULES:
        supports_history(kind)  # must not raise for any registered kind


def test_every_history_capable_kind_is_handled_by_rederive():
    """`_normalize` is the one per-source chain both `rederive` (facts) and
    `relabel` (tags) read, dispatching on the source name. A source that can
    fetch history but is missing from it silently returns no facts and no
    tags on reindex — indistinguishable from a source that has none.

    Checked structurally rather than by calling it: each branch needs a
    well-formed raw record of its own shape, and inventing five of them would
    test the fixtures, not the dispatch. Both readers are pinned to the chain
    so a second copy cannot quietly grow beside it again."""
    import inspect

    from brief.sources import MODULES, _normalize, rederive, relabel, supports_history

    source = inspect.getsource(_normalize)
    for kind in MODULES:
        if not supports_history(kind):
            continue
        assert f'"{kind}"' in source, (
            f"{kind} fetches history but _normalize() never names it, so "
            "brief reindex would leave its rows underived and untagged"
        )
    assert "_normalize(" in inspect.getsource(rederive)
    assert "_normalize(" in inspect.getsource(relabel)


def test_relabel_reads_each_adapters_labels():
    """One representative raw record per label-bearing source, through the
    real normalizer: the labels a reindex would see."""
    from brief.sources import relabel

    openalex = {
        "id": "https://openalex.org/W1", "title": "A pangenome", "display_name": "A pangenome",
        "publication_date": "2026-09-01", "authorships": [], "primary_location": {},
        "concepts": [
            {"display_name": "Genomics", "score": 0.7},
            {"display_name": "Cable gland", "score": 0.1},
        ],
        "topics": [{"display_name": "Plant Genomics", "subfield": {"display_name": "Genetics"}}],
        "keywords": [{"display_name": "Pangenome"}],
    }
    assert relabel("openalex", openalex) == ["Genomics", "Plant Genomics", "Genetics", "Pangenome"]
    github = {"full_name": "o/r", "html_url": "https://github.com/o/r", "topics": ["bioinformatics", "rust"],
              "language": "Rust", "stargazers_count": 1, "pushed_at": "2026-09-01T00:00:00Z",
              "created_at": "2026-01-01T00:00:00Z", "owner": {"login": "o"}}
    assert relabel("github_repos", github) == ["bioinformatics", "rust", "Rust"]
    assert relabel("feed_a", {"anything": 1}) == []
