"""Tests for the openalex-works-search seed.

Real sockets through tests/harness/stub_http.py, and a real trimmed API
response in tests/fixtures/openalex_works.json — captured from the live
service, carrying a work with an abstract, one without, and one with no DOI.
No mocked urlopen: the bugs in this module's class live in URL construction,
cursor handling and response shaping, and a mock tests none of them.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from brief.lib import openalex_works_search as oa
from harness.stub_http import Response
from harness.stub_http import stub_http as stub_http_server  # noqa: F401

FIXTURES = Path(__file__).resolve().parent / "fixtures"
WORKS = json.loads((FIXTURES / "openalex_works.json").read_text(encoding="utf-8"))
MAIL = "t@example.test"


class _PathOnlyRoutes(dict):
    """Route on path, ignoring the query — the query is what is under test."""

    def __contains__(self, key) -> bool:
        method, target = key
        return dict.__contains__(self, (method, urlsplit(target).path))

    def __getitem__(self, key):
        method, target = key
        return dict.__getitem__(self, (method, urlsplit(target).path))


@pytest.fixture
def stub(stub_http_server):
    stub_http_server.routes = _PathOnlyRoutes()
    return stub_http_server


def query_of(request) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(request.path).query).items()}


def hits(stub, path: str) -> list:
    """Recorded requests for a path. `requests_to` keys on the raw target,
    query string included, so it never matches a query we are testing."""
    return [r for r in stub.requests if urlsplit(r.path).path == path]


def search(stub, **kw):
    kw.setdefault("institution_id", "I173911158")
    kw.setdefault("from_date", "2026-09-01")
    kw.setdefault("to_date", "2026-09-17")
    kw.setdefault("mailto", MAIL)
    kw["base_url"] = stub.url
    return oa.search_openalex_works(**kw)


# ------------------------------------------------------------ institution ---


@pytest.mark.parametrize(
    "given,expected",
    [
        ("I173911158", "I173911158"),
        ("i173911158", "I173911158"),
        ("https://openalex.org/I173911158", "I173911158"),
        ("04rswrd78", "https://ror.org/04rswrd78"),
        ("https://ror.org/04rswrd78", "https://ror.org/04rswrd78"),
    ],
)
def test_an_id_is_accepted_in_every_spelling(given, expected):
    assert oa.normalize_institution_id(given) == expected


def test_a_bare_name_is_refused_with_the_reason():
    """Measured: searching "Iowa State University" returns the university,
    its digital press, and the Plant Sciences Institute. Taking [0] silently
    is how a brief ends up about a different body."""
    with pytest.raises(ValueError) as caught:
        oa.normalize_institution_id("Iowa State University")
    assert "resolve_institution" in str(caught.value)


def test_resolve_institution_returns_candidates_and_chooses_none(stub):
    stub.route("/institutions", json.dumps({"results": [
        {"id": "https://openalex.org/I1", "display_name": "A Univ", "works_count": 9,
         "ror": "https://ror.org/aaaaaaaaa", "type": "education", "country_code": "US"},
        {"id": "https://openalex.org/I2", "display_name": "A Univ Press", "works_count": 0,
         "ror": "", "type": "other", "country_code": "US"},
    ]}).encode())

    found = oa.resolve_institution("A Univ", mailto=MAIL, base_url=stub.url)

    assert [c["id"] for c in found] == ["I1", "I2"], "both, in the order given"


# ----------------------------------------------------------------- request ---


def test_the_filter_uses_lineage_so_sub_units_are_included(stub):
    """institutions.id alone drops a work credited to a department or
    institute inside the university. This is the whole reason for lineage."""
    stub.route("/works", json.dumps({"meta": {"next_cursor": None}, "results": []}).encode())

    search(stub)

    sent = query_of(hits(stub, "/works")[-1])["filter"]
    assert "authorships.institutions.lineage:I173911158" in sent
    assert "authorships.institutions.id:" not in sent


def test_both_ends_of_the_date_window_are_sent(stub):
    stub.route("/works", json.dumps({"meta": {"next_cursor": None}, "results": []}).encode())

    search(stub, from_date="2026-01-01", to_date="2026-01-31")

    sent = query_of(hits(stub, "/works")[-1])["filter"]
    assert "from_publication_date:2026-01-01" in sent
    assert "to_publication_date:2026-01-31" in sent


def test_the_contact_address_reaches_the_server(stub):
    """It is what buys the polite pool. Sending it in the UA is the
    documented convention."""
    stub.route("/works", json.dumps({"meta": {"next_cursor": None}, "results": []}).encode())

    search(stub)

    ua = hits(stub, "/works")[-1].headers.get("User-Agent", "")
    assert f"mailto:{MAIL}" in ua


def test_a_missing_contact_address_is_refused_before_any_request(stub):
    stub.route("/works", b"{}")

    with pytest.raises(ValueError):
        search(stub, mailto="")

    assert not hits(stub, "/works"), "no request should have been made"


def test_search_is_omitted_unless_asked_for(stub):
    """This app filters locally against a visible, versioned keyword list.
    An opaque relevance filter on top would be a second filter nobody can
    inspect."""
    stub.route("/works", json.dumps({"meta": {"next_cursor": None}, "results": []}).encode())

    search(stub)
    assert "search" not in query_of(hits(stub, "/works")[-1])

    search(stub, search="genomics")
    assert query_of(hits(stub, "/works")[-1])["search"] == "genomics"


# ----------------------------------------------------------------- shaping ---


def test_a_real_response_shapes_into_flat_records(stub):
    stub.route("/works", json.dumps(WORKS).encode())

    works = search(stub)

    assert len(works) == len(WORKS["results"])
    for w in works:
        assert set(w) == {
            "external_id", "url", "title", "abstract", "journal", "authors",
            "first_author", "doi", "pmid", "published_at", "type", "raw",
        }, "every key present on every record, empty rather than missing"


def test_the_abstract_is_rebuilt_from_the_inverted_index(stub):
    stub.route("/works", json.dumps(WORKS).encode())

    works = search(stub)
    with_abstract = [w for w in works if w["abstract"]]

    assert with_abstract, "the fixture carries at least one"
    assert " " in with_abstract[0]["abstract"]


def test_a_work_with_no_abstract_yields_an_empty_string_not_a_crash(stub):
    stub.route("/works", json.dumps(WORKS).encode())

    works = search(stub)

    assert any(w["abstract"] == "" for w in works), "the fixture carries one"


def test_positions_are_honoured_not_iteration_order():
    """The index is a mapping; its order is not the sentence order."""
    index = {"world": [1, 2], "Hello": [0], "again": [3]}
    assert oa.reconstruct_abstract(index) == "Hello world world again"


def test_a_missing_or_malformed_index_is_empty_rather_than_an_error():
    assert oa.reconstruct_abstract(None) == ""
    assert oa.reconstruct_abstract({}) == ""
    assert oa.reconstruct_abstract({"x": "not a list"}) == ""


def test_a_work_without_a_doi_still_gets_a_url(stub):
    """The fixture carries one. A record with no url is unusable downstream —
    the citation rule needs somewhere to point."""
    stub.route("/works", json.dumps(WORKS).encode())

    for w in search(stub):
        assert w["url"], f"{w['external_id']} has no url"


# ------------------------------------------------------------------ paging ---


def test_the_cursor_walks_until_the_window_is_covered(stub):
    pages = [
        {"meta": {"next_cursor": "second"}, "results": [{"id": "https://openalex.org/W1"}]},
        {"meta": {"next_cursor": "third"}, "results": [{"id": "https://openalex.org/W2"}]},
        {"meta": {"next_cursor": None}, "results": [{"id": "https://openalex.org/W3"}]},
    ]
    seen = []

    def handler(request):
        seen.append(query_of(request).get("cursor"))
        return json.dumps(pages[len(seen) - 1]).encode()

    stub.route("/works", handler)

    works = search(stub)

    assert [w["external_id"] for w in works] == ["W1", "W2", "W3"]
    assert seen == ["*", "second", "third"], "the cursor is carried forward"


def test_max_pages_bounds_the_walk_and_returns_what_it_has(stub):
    """A wide window must not page forever. Reaching the bound returns the
    partial result rather than raising — the caller decides if that matters."""
    def handler(request):
        return json.dumps({
            "meta": {"next_cursor": "more"},
            "results": [{"id": f"https://openalex.org/W{len(hits(stub, '/works'))}"}],
        }).encode()

    stub.route("/works", handler)

    works = search(stub, max_pages=3)

    assert len(hits(stub, "/works")) == 3
    assert len(works) == 3


def test_a_record_repeated_across_pages_is_returned_once(stub):
    """A cursor walk can repeat a record when the index shifts under it."""
    pages = [
        {"meta": {"next_cursor": "b"}, "results": [{"id": "https://openalex.org/W1"}]},
        {"meta": {"next_cursor": None},
         "results": [{"id": "https://openalex.org/W1"}, {"id": "https://openalex.org/W2"}]},
    ]
    calls = []

    def handler(request):
        calls.append(1)
        return json.dumps(pages[len(calls) - 1]).encode()

    stub.route("/works", handler)

    assert [w["external_id"] for w in search(stub)] == ["W1", "W2"]


def test_an_empty_first_page_stops_immediately(stub):
    stub.route("/works", json.dumps({"meta": {"next_cursor": "x"}, "results": []}).encode())

    assert search(stub) == []
    assert len(hits(stub, "/works")) == 1


@pytest.mark.parametrize("bad", [0, -1, 201])
def test_an_out_of_range_per_page_is_refused_before_any_request(stub, bad):
    stub.route("/works", b"{}")
    with pytest.raises(ValueError):
        search(stub, per_page=bad)
    assert not hits(stub, "/works")


# ------------------------------------------------------------------ errors ---


def test_a_non_200_names_the_status(stub):
    stub.route("/works", Response(b"nope", status=500))

    with pytest.raises(oa.OpenAlexError) as caught:
        search(stub)

    assert "500" in str(caught.value)


def test_an_unparseable_body_is_an_error_not_a_silent_empty_result(stub):
    stub.route("/works", b"<html>not json</html>")

    with pytest.raises(oa.OpenAlexError):
        search(stub)


def test_a_json_array_is_refused_rather_than_shaped(stub):
    stub.route("/works", b"[]")

    with pytest.raises(oa.OpenAlexError):
        search(stub)


def test_a_gzip_body_is_decompressed(stub):
    """urllib does not decompress; requests does. Left undone, .decode()
    returns replacement characters instead of raising."""
    stub.route("/works", Response(
        gzip.compress(json.dumps(WORKS).encode()),
        headers={"Content-Encoding": "gzip"},
    ))

    assert len(search(stub)) == len(WORKS["results"])


def test_a_bot_wall_is_named_rather_than_parsed(stub):
    stub.route("/works", Response(
        b"<html><head><title>Just a moment...</title></head></html>",
        status=403, headers={"Server": "cloudflare"},
    ))

    with pytest.raises(oa.OpenAlexBlockedError) as caught:
        search(stub)

    assert "bot wall" in str(caught.value)
    assert "Not retried" in str(caught.value)


def test_an_unreachable_server_raises_the_modules_own_error():
    with pytest.raises(oa.OpenAlexError):
        oa.search_openalex_works(
            "I173911158", from_date="2026-09-01", to_date="2026-09-17",
            mailto=MAIL, base_url="http://127.0.0.1:1",
        )


def test_a_non_http_base_url_is_refused():
    with pytest.raises(oa.OpenAlexError):
        oa.search_openalex_works(
            "I173911158", from_date="2026-09-01", to_date="2026-09-17",
            mailto=MAIL, base_url="file:///etc",
        )


# -------------------------------------------------------------- boundaries ---


def test_the_module_imports_only_the_standard_library():
    import ast

    tree = ast.parse(Path(oa.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                roots.add(f"<relative import level {node.level}>")
            elif node.module:
                roots.add(node.module.split(".")[0])

    assert roots == {"__future__", "json", "re", "urllib", "typing", "gzip", "zlib"}


def test_the_seed_does_not_import_from_the_app():
    source = Path(oa.__file__).read_text(encoding="utf-8")
    assert "from brief" not in source and "import brief" not in source


# ------------------------------- boundary change: topic-scoped searching ---


def test_neither_an_institution_nor_a_search_is_refused(stub):
    """The resulting query is every work OpenAlex holds in the window —
    hundreds of thousands for a month. Left to run it fails as a timeout or
    a truncated page, which looks nothing like the mistake it is."""
    stub.route("/works", b"{}")

    with pytest.raises(ValueError) as caught:
        oa.search_openalex_works(
            from_date="2026-09-01", to_date="2026-09-17",
            mailto=MAIL, base_url=stub.url,
        )

    assert "every work" in str(caught.value)
    assert not hits(stub, "/works"), "and nothing was requested"


def test_a_search_alone_queries_the_whole_corpus_in_the_window(stub):
    """A topic profile has no institution to narrow by."""
    stub.route("/works", json.dumps({"meta": {"next_cursor": None}, "results": []}).encode())

    oa.search_openalex_works(
        search="rust bioinformatics", from_date="2026-09-01", to_date="2026-09-17",
        mailto=MAIL, base_url=stub.url,
    )

    sent = query_of(hits(stub, "/works")[-1])
    assert sent["search"] == "rust bioinformatics"
    assert "lineage" not in sent["filter"], "no institution filter is sent"
    assert "from_publication_date:2026-09-01" in sent["filter"], "the window still bounds it"


def test_an_institution_and_a_search_together_send_both(stub):
    stub.route("/works", json.dumps({"meta": {"next_cursor": None}, "results": []}).encode())

    search(stub, search="genomics")

    sent = query_of(hits(stub, "/works")[-1])
    assert "authorships.institutions.lineage:I173911158" in sent["filter"]
    assert sent["search"] == "genomics"


def test_a_blank_institution_is_the_same_as_none(stub):
    """An empty string from a YAML key that exists but is unset must not
    read as 'an institution was given'."""
    stub.route("/works", b"{}")

    with pytest.raises(ValueError):
        oa.search_openalex_works(
            "   ", from_date="2026-09-01", to_date="2026-09-17",
            mailto=MAIL, base_url=stub.url,
        )


def test_an_institution_alone_still_needs_no_search(stub):
    """The original contract, unchanged: this app filters locally against a
    visible keyword list rather than OpenAlex's opaque relevance."""
    stub.route("/works", json.dumps({"meta": {"next_cursor": None}, "results": []}).encode())

    search(stub)

    assert "search" not in query_of(hits(stub, "/works")[-1])


def test_a_blank_institution_alongside_a_search_is_a_topic_query(stub):
    """The path the earlier tests missed. A YAML key that exists but is
    empty — `institution: ""` — must read as "no institution", not as an
    institution that fails to normalise. With a search present the guard
    does not fire, so only this pins it."""
    stub.route("/works", json.dumps({"meta": {"next_cursor": None}, "results": []}).encode())

    oa.search_openalex_works(
        "   ", search="rust bioinformatics",
        from_date="2026-09-01", to_date="2026-09-17",
        mailto=MAIL, base_url=stub.url,
    )

    sent = query_of(hits(stub, "/works")[-1])
    assert "lineage" not in sent["filter"]
    assert sent["search"] == "rust bioinformatics"
