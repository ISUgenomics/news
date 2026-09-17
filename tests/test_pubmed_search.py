"""Tests for lib/pubmed_search.py.

Every HTTP path runs against a real stub server on an ephemeral port
(``tests/harness/stub_http.py``) — no mocks, no live network. Golden bodies in
``tests/fixtures/pubmed_search_*.json`` were captured from the live
E-utilities endpoints once, at boundary-writing time, and are replayed here.
"""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from brief.lib import pubmed_search as ps
from harness.stub_http import Response, unreachable_url  # noqa: F401
from harness.stub_http import stub_http as stub_http_server  # noqa: F401

FIXTURES = Path(__file__).resolve().parent / "fixtures"

ESEARCH_PATH = "/esearch.fcgi"
ESUMMARY_PATH = "/esummary.fcgi"

EMAIL = "someone@example.org"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> dict:
    return json.loads(fixture_bytes(name))


def query_of(request) -> dict[str, list[str]]:
    """Parsed query string of a recorded stub request."""
    return parse_qs(urlsplit(request.path).query)


def path_of(request) -> str:
    return urlsplit(request.path).path


class _PathOnlyRoutes(dict):
    """Route table that matches on the path, ignoring the query string.

    ``stub_http`` keys its routes on the raw request target, query string
    included; every E-utilities parameter lives in that query, so an exact-target
    route table would mean spelling out the encoding under test. Only the route
    *lookup* changes — the server, the socket and the recorded requests are the
    harness's own.
    """

    def __contains__(self, key) -> bool:
        method, target = key
        return dict.__contains__(self, (method, urlsplit(target).path))

    def __getitem__(self, key):
        method, target = key
        return dict.__getitem__(self, (method, urlsplit(target).path))


@pytest.fixture
def stub_http(stub_http_server):
    stub_http_server.routes = _PathOnlyRoutes()
    return stub_http_server


def route_search_and_summary(stub) -> None:
    stub.route(ESEARCH_PATH, fixture_bytes("pubmed_search_esearch.json"))
    stub.route(ESUMMARY_PATH, fixture_bytes("pubmed_search_esummary.json"))


# --------------------------------------------------------------- esearch ---


def test_esearch_sends_documented_request_shape(stub_http):
    stub_http.route(ESEARCH_PATH, fixture_bytes("pubmed_search_esearch.json"))

    ps.esearch_ids(
        "maize[Title] AND (genome OR GWAS)",
        email=EMAIL,
        max_results=3,
        base_url=stub_http.url,
        tool="unit-test",
    )

    request = stub_http.last()
    assert request.method == "GET"
    assert path_of(request) == ESEARCH_PATH
    query = query_of(request)
    assert query["db"] == ["pubmed"]
    assert query["term"] == ["maize[Title] AND (genome OR GWAS)"]
    assert query["retmode"] == ["json"]
    assert query["retmax"] == ["3"]
    assert query["retstart"] == ["0"]
    assert query["tool"] == ["unit-test"]
    assert query["email"] == [EMAIL]
    # no key was supplied, so none must be sent
    assert "api_key" not in query
    # no window was supplied, so no date parameters at all
    assert "datetype" not in query
    assert "mindate" not in query
    assert "maxdate" not in query


def test_esearch_sends_date_window_and_api_key_when_given(stub_http):
    stub_http.route(ESEARCH_PATH, fixture_bytes("pubmed_search_esearch.json"))

    ps.esearch_ids(
        "maize[Title]",
        email=EMAIL,
        mindate="2024/01/01",
        maxdate="2024/03/01",
        base_url=stub_http.url,
        api_key="deadbeef",
    )

    query = query_of(stub_http.last())
    assert query["datetype"] == ["edat"]
    assert query["mindate"] == ["2024/01/01"]
    assert query["maxdate"] == ["2024/03/01"]
    assert query["api_key"] == ["deadbeef"]


def test_esearch_honours_a_non_default_datetype(stub_http):
    stub_http.route(ESEARCH_PATH, fixture_bytes("pubmed_search_esearch.json"))

    ps.esearch_ids(
        "maize[Title]",
        email=EMAIL,
        mindate="2024",
        maxdate="2025",
        datetype="pdat",
        base_url=stub_http.url,
    )

    assert query_of(stub_http.last())["datetype"] == ["pdat"]


def test_esearch_sends_user_agent_header_when_given(stub_http):
    stub_http.route(ESEARCH_PATH, fixture_bytes("pubmed_search_esearch.json"))

    ps.esearch_ids(
        "maize[Title]",
        email=EMAIL,
        base_url=stub_http.url,
        user_agent="topic-brief/0.1 (+https://example.org)",
    )

    headers = {k.lower(): v for k, v in stub_http.last().headers.items()}
    assert headers["user-agent"] == "topic-brief/0.1 (+https://example.org)"


def test_esearch_percent_encodes_special_characters_in_the_term(stub_http):
    stub_http.route(ESEARCH_PATH, fixture_bytes("pubmed_search_esearch.json"))
    term = 'Schön C[Author] AND "maïs & blé"[Title] AND 1/2'

    ps.esearch_ids(term, email=EMAIL, base_url=stub_http.url)

    request = stub_http.last()
    # raw path must be escaped, and must round-trip to exactly the term given
    assert "Schön" not in request.path
    assert " " not in request.path
    assert query_of(request)["term"] == [term]


def test_esearch_returns_the_id_list_from_the_envelope(stub_http):
    stub_http.route(ESEARCH_PATH, fixture_bytes("pubmed_search_esearch.json"))

    ids = ps.esearch_ids("maize[Title]", email=EMAIL, base_url=stub_http.url)

    assert ids == ["38427914", "38427606", "38426620"]
    assert all(isinstance(pmid, str) for pmid in ids)


def test_esearch_returns_empty_list_on_zero_hits(stub_http):
    stub_http.route(ESEARCH_PATH, fixture_bytes("pubmed_search_esearch_zero.json"))

    assert ps.esearch_ids("nope[Title]", email=EMAIL, base_url=stub_http.url) == []


def test_esearch_truncates_to_max_results_if_ncbi_overshoots(stub_http):
    stub_http.route(ESEARCH_PATH, fixture_bytes("pubmed_search_esearch.json"))

    ids = ps.esearch_ids(
        "maize[Title]", email=EMAIL, max_results=2, base_url=stub_http.url
    )

    assert ids == ["38427914", "38427606"]


def test_esearch_raises_on_ncbi_error_key_despite_http_200(stub_http):
    stub_http.route(ESEARCH_PATH, fixture_bytes("pubmed_search_esearch_error.json"))

    with pytest.raises(RuntimeError) as excinfo:
        ps.esearch_ids("maize[Title]", email=EMAIL, base_url=stub_http.url)

    assert "Invalid db name specified: nosuchdb" in str(excinfo.value)


def test_esearch_raises_on_non_200(stub_http):
    stub_http.route(ESEARCH_PATH, Response(body=b"upstream exploded", status=502))

    with pytest.raises(RuntimeError) as excinfo:
        ps.esearch_ids("maize[Title]", email=EMAIL, base_url=stub_http.url)

    assert "502" in str(excinfo.value)


def test_esearch_raises_on_malformed_body(stub_http):
    stub_http.route(ESEARCH_PATH, b"<!DOCTYPE html><h1>Bad Gateway</h1>")

    with pytest.raises(RuntimeError) as excinfo:
        ps.esearch_ids("maize[Title]", email=EMAIL, base_url=stub_http.url)

    assert "json" in str(excinfo.value).lower()


def test_esearch_raises_on_json_that_is_not_an_esearch_envelope(stub_http):
    stub_http.route(ESEARCH_PATH, {"header": {"type": "esearch"}})

    with pytest.raises(RuntimeError):
        ps.esearch_ids("maize[Title]", email=EMAIL, base_url=stub_http.url)


def test_esearch_raises_on_connection_failure(unreachable_url):
    with pytest.raises(RuntimeError) as excinfo:
        ps.esearch_ids("maize[Title]", email=EMAIL, base_url=unreachable_url)

    assert "esearch" in str(excinfo.value).lower()


def test_esearch_raises_on_timeout(stub_http):
    def slow(request):
        time.sleep(1.5)
        return {"esearchresult": {"idlist": []}}

    stub_http.route(ESEARCH_PATH, slow)

    with pytest.raises(RuntimeError):
        ps.esearch_ids(
            "maize[Title]", email=EMAIL, base_url=stub_http.url, timeout_s=0.25
        )


# ------------------------------------------------- argument validation ---


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mindate": "2024/01/01"},
        {"maxdate": "2024/03/01"},
    ],
)
def test_one_sided_date_window_is_rejected_before_any_request(stub_http, kwargs):
    stub_http.route(ESEARCH_PATH, fixture_bytes("pubmed_search_esearch.json"))

    with pytest.raises(ValueError):
        ps.esearch_ids("maize[Title]", email=EMAIL, base_url=stub_http.url, **kwargs)

    assert stub_http.requests == []


@pytest.mark.parametrize(
    "bad", ["2024-01-01", "01/01/2024", "2024/1/1", "yesterday", ""]
)
def test_malformed_date_is_rejected_before_any_request(stub_http, bad):
    stub_http.route(ESEARCH_PATH, fixture_bytes("pubmed_search_esearch.json"))

    with pytest.raises(ValueError):
        ps.esearch_ids(
            "maize[Title]",
            email=EMAIL,
            mindate=bad,
            maxdate="2024/03/01",
            base_url=stub_http.url,
        )

    assert stub_http.requests == []


@pytest.mark.parametrize("bad", [0, -1, 10001])
def test_out_of_range_max_results_is_rejected_before_any_request(stub_http, bad):
    stub_http.route(ESEARCH_PATH, fixture_bytes("pubmed_search_esearch.json"))

    with pytest.raises(ValueError):
        ps.esearch_ids(
            "maize[Title]", email=EMAIL, max_results=bad, base_url=stub_http.url
        )

    assert stub_http.requests == []


def test_blank_query_is_rejected_before_any_request(stub_http):
    stub_http.route(ESEARCH_PATH, fixture_bytes("pubmed_search_esearch.json"))

    with pytest.raises(ValueError):
        ps.esearch_ids("   ", email=EMAIL, base_url=stub_http.url)

    assert stub_http.requests == []


def test_blank_email_is_rejected_before_any_request(stub_http):
    stub_http.route(ESEARCH_PATH, fixture_bytes("pubmed_search_esearch.json"))

    with pytest.raises(ValueError):
        ps.esearch_ids("maize[Title]", email="  ", base_url=stub_http.url)

    assert stub_http.requests == []


# -------------------------------------------------------------- esummary ---


def test_esummary_uses_get_with_comma_joined_ids_up_to_the_limit(stub_http):
    stub_http.route(ESUMMARY_PATH, fixture_bytes("pubmed_search_esummary.json"))

    ps.esummary_records(
        ["38427914", "38427606", "38426620"],
        email=EMAIL,
        base_url=stub_http.url,
        tool="unit-test",
    )

    request = stub_http.last()
    assert request.method == "GET"
    assert path_of(request) == ESUMMARY_PATH
    assert request.body == b""
    query = query_of(request)
    assert query["db"] == ["pubmed"]
    assert query["id"] == ["38427914,38427606,38426620"]
    assert query["retmode"] == ["json"]
    assert query["tool"] == ["unit-test"]
    assert query["email"] == [EMAIL]


def test_esummary_switches_to_post_above_the_get_id_limit(stub_http):
    pmids = [str(900000 + n) for n in range(ps.GET_ID_LIMIT + 1)]
    stub_http.route(
        ESUMMARY_PATH,
        {"result": {"uids": [], **{}}},
    )

    ps.esummary_records(pmids, email=EMAIL, base_url=stub_http.url)

    request = stub_http.last()
    assert request.method == "POST"
    assert path_of(request) == ESUMMARY_PATH
    # the ids must be in the body, not the URL
    assert urlsplit(request.path).query == ""
    body = parse_qs(request.body.decode())
    assert body["id"] == [",".join(pmids)]
    assert body["db"] == ["pubmed"]
    assert body["retmode"] == ["json"]
    assert body["email"] == [EMAIL]
    headers = {k.lower(): v for k, v in request.headers.items()}
    assert headers["content-type"] == "application/x-www-form-urlencoded"


def test_esummary_stays_on_get_at_exactly_the_id_limit(stub_http):
    pmids = [str(900000 + n) for n in range(ps.GET_ID_LIMIT)]
    stub_http.route(ESUMMARY_PATH, {"result": {"uids": []}})

    ps.esummary_records(pmids, email=EMAIL, base_url=stub_http.url)

    assert stub_http.last().method == "GET"


def test_esummary_returns_records_in_uids_order(stub_http):
    stub_http.route(ESUMMARY_PATH, fixture_bytes("pubmed_search_esummary.json"))

    records = ps.esummary_records(
        ["38426620", "38427914", "38427606"], email=EMAIL, base_url=stub_http.url
    )

    assert [r["uid"] for r in records] == ["38427914", "38427606", "38426620"]


def test_esummary_with_no_ids_makes_no_request(stub_http):
    stub_http.route(ESUMMARY_PATH, fixture_bytes("pubmed_search_esummary.json"))

    assert ps.esummary_records([], email=EMAIL, base_url=stub_http.url) == []
    assert stub_http.requests == []


def test_esummary_rejects_a_bare_string_of_ids_before_any_request(stub_http):
    stub_http.route(ESUMMARY_PATH, fixture_bytes("pubmed_search_esummary.json"))

    with pytest.raises(TypeError):
        ps.esummary_records("38427914", email=EMAIL, base_url=stub_http.url)

    assert stub_http.requests == []


def test_esummary_raises_on_a_per_record_error_rather_than_returning_partial(stub_http):
    stub_http.route(ESUMMARY_PATH, fixture_bytes("pubmed_search_esummary_baduid.json"))

    with pytest.raises(RuntimeError) as excinfo:
        ps.esummary_records(["99999999999"], email=EMAIL, base_url=stub_http.url)

    message = str(excinfo.value)
    assert "99999999999" in message
    assert "cannot get document summary" in message


def test_esummary_raises_on_envelope_error_key(stub_http):
    stub_http.route(ESUMMARY_PATH, {"error": "API rate limit exceeded"})

    with pytest.raises(RuntimeError) as excinfo:
        ps.esummary_records(["38427914"], email=EMAIL, base_url=stub_http.url)

    assert "API rate limit exceeded" in str(excinfo.value)


def test_esummary_raises_on_non_200(stub_http):
    stub_http.route(ESUMMARY_PATH, Response(body=b"nope", status=429))

    with pytest.raises(RuntimeError) as excinfo:
        ps.esummary_records(["38427914"], email=EMAIL, base_url=stub_http.url)

    assert "429" in str(excinfo.value)


def test_esummary_raises_on_malformed_body(stub_http):
    stub_http.route(ESUMMARY_PATH, b"not json at all")

    with pytest.raises(RuntimeError):
        ps.esummary_records(["38427914"], email=EMAIL, base_url=stub_http.url)


def test_esummary_raises_on_connection_failure(unreachable_url):
    with pytest.raises(RuntimeError) as excinfo:
        ps.esummary_records(["38427914"], email=EMAIL, base_url=unreachable_url)

    assert "esummary" in str(excinfo.value).lower()


def test_esummary_decodes_utf8_off_the_wire(stub_http):
    stub_http.route(ESUMMARY_PATH, fixture_bytes("pubmed_search_esummary_unicode.json"))

    records = ps.esummary_records(["40000001"], email=EMAIL, base_url=stub_http.url)

    assert records[0]["title"].startswith("Effet des µmol photons m⁻² s⁻¹")
    assert records[0]["authors"][0]["name"] == "Schön CC"


# -------------------------------------------------------- parse_pubdate ---


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026 Sep 3", "2026-09-03"),
        ("2024 May 7", "2024-05-07"),
        ("2026 Sep", "2026-09-01"),
        ("2024 Jul", "2024-07-01"),
        ("2026", "2026-01-01"),
        # a month range takes its FIRST month, which for Jan-Feb happens to
        # coincide with the year default -- Nov-Dec is the case that tells them
        # apart, so both are pinned here
        ("2024 Jan-Feb", "2024-01-01"),
        ("2024 Nov-Dec", "2024-11-01"),
        ("2024 Sept 3", "2024-09-03"),
        # a season is not a month: degrade to the year
        ("2024 Winter", "2024-01-01"),
        ("2024 Dec 31", "2024-12-31"),
    ],
)
def test_parse_pubdate_maps_the_documented_forms(text, expected):
    assert ps.parse_pubdate(text) == expected


@pytest.mark.parametrize(
    "text", ["", "   ", "no year here", "Sep 2026", "24 Sep 3", "20245", "202"]
)
def test_parse_pubdate_returns_none_on_unparseable_text(text):
    assert ps.parse_pubdate(text) is None


def test_parse_pubdate_rejects_an_impossible_day():
    assert ps.parse_pubdate("2024 Feb 31") is None


def test_parse_pubdate_drops_the_day_of_an_unrecognised_month():
    # "Jam" is not a month; keeping the 15 would date the record confidently
    # and wrongly in January. The year is the honest answer.
    assert ps.parse_pubdate("2024 Jam 15") == "2024-01-01"


# -------------------------------------------------------- record_to_item ---


def test_record_to_item_maps_a_golden_record():
    record = fixture_json("pubmed_search_esummary.json")["result"]["38426620"]

    item = ps.record_to_item(record)

    assert item["external_id"] == "38426620"
    assert item["url"] == "https://pubmed.ncbi.nlm.nih.gov/38426620/"
    assert item["title"].startswith("Maize functional requirements drive")
    assert item["journal"] == "New Phytol"
    assert item["authors"][:3] == ["Zhang L", "Yuan L", "Wen Y"]
    assert item["volume"] == "242"
    assert item["issue"] == "3"
    assert item["pages"] == "1275-1288"
    assert item["doi"] == "10.1111/nph.19653"
    assert item["published_at"] == "2024-05-01"
    assert item["raw"] is record


def test_record_to_item_prefers_pubdate_and_falls_back_to_epubdate():
    base = {"uid": "1", "pubdate": "", "epubdate": "2024 Mar 1"}
    assert ps.record_to_item(base)["published_at"] == "2024-03-01"

    base = {"uid": "1", "pubdate": "2024 May 7", "epubdate": "2024 Mar 1"}
    assert ps.record_to_item(base)["published_at"] == "2024-05-07"

    base = {"uid": "1", "pubdate": "n/a", "epubdate": "2024 Mar 1"}
    assert ps.record_to_item(base)["published_at"] == "2024-03-01"


def test_record_to_item_tolerates_a_bare_record():
    item = ps.record_to_item({"uid": "7"})

    assert item == {
        "external_id": "7",
        "url": "https://pubmed.ncbi.nlm.nih.gov/7/",
        "title": "",
        "journal": "",
        "authors": [],
        "volume": "",
        "issue": "",
        "pages": "",
        "doi": None,
        "published_at": None,
        "raw": {"uid": "7"},
    }


def test_record_to_item_takes_doi_from_elocationid_when_articleids_lack_one():
    record = {
        "uid": "9",
        "articleids": [{"idtype": "pubmed", "value": "9"}],
        "elocationid": "doi: 10.1000/xyz123",
    }

    assert ps.record_to_item(record)["doi"] == "10.1000/xyz123"


def test_record_to_item_leaves_doi_none_when_elocationid_is_not_a_doi():
    record = {"uid": "9", "articleids": [], "elocationid": "e12345"}

    assert ps.record_to_item(record)["doi"] is None


def test_record_to_item_keeps_unicode_intact():
    record = fixture_json("pubmed_search_esummary_unicode.json")["result"]["40000001"]

    item = ps.record_to_item(record)

    assert "β-carotène du maïs" in item["title"]
    assert item["authors"] == ["Schön CC", "Müller-Røed Ø"]
    assert item["published_at"] == "2026-09-03"


def test_record_to_item_rejects_a_record_without_a_uid():
    with pytest.raises(ValueError):
        ps.record_to_item({"title": "orphan"})


# --------------------------------------------------------- citation_line ---


def test_citation_line_joins_the_parts_it_has():
    record = fixture_json("pubmed_search_esummary.json")["result"]["38426620"]

    line = ps.citation_line(record)

    # 22 authors on this record, so NLM-style truncation applies
    assert line == (
        "Zhang L, Yuan L, Wen Y, et al. "
        "New Phytol. 2024 May;242(3):1275-1288. doi:10.1111/nph.19653"
    )


def test_citation_line_accepts_the_item_dict_as_well_as_the_record():
    """The caller of search_pubmed holds item dicts, not esummary records."""
    record = fixture_json("pubmed_search_esummary.json")["result"]["38426620"]

    from_item = ps.citation_line(ps.record_to_item(record))

    assert from_item == ps.citation_line(record)
    # the parts that only live in the raw record must survive the hop
    assert "New Phytol" in from_item
    assert "2024 May" in from_item
    assert "doi:10.1111/nph.19653" in from_item


def test_citation_line_lists_up_to_six_authors_in_full():
    record = {
        "uid": "1",
        "authors": [{"name": f"A{n} X"} for n in range(6)],
        "source": "J Test",
    }

    assert ps.citation_line(record) == ("A0 X, A1 X, A2 X, A3 X, A4 X, A5 X. J Test")


def test_citation_line_truncates_above_six_authors():
    record = {
        "uid": "1",
        "authors": [{"name": f"A{n} X"} for n in range(7)],
        "source": "J Test",
    }

    assert ps.citation_line(record) == "A0 X, A1 X, A2 X, et al. J Test"


def test_citation_line_omits_missing_parts_without_stray_separators():
    line = ps.citation_line({"uid": "1", "source": "J Test", "pubdate": "2024"})

    assert line == "J Test. 2024"


def test_citation_line_of_an_empty_record_is_empty():
    assert ps.citation_line({"uid": "1"}) == ""


def test_citation_line_is_one_line():
    record = fixture_json("pubmed_search_esummary_unicode.json")["result"]["40000001"]

    assert "\n" not in ps.citation_line(record)


# --------------------------------------------------------- search_pubmed ---


def test_search_pubmed_makes_both_calls_and_returns_items(stub_http):
    route_search_and_summary(stub_http)

    items = ps.search_pubmed(
        "maize[Title] AND genome",
        email=EMAIL,
        mindate="2024/01/01",
        maxdate="2024/03/01",
        max_results=3,
        base_url=stub_http.url,
    )

    assert [path_of(r) for r in stub_http.requests] == [ESEARCH_PATH, ESUMMARY_PATH]
    assert [i["external_id"] for i in items] == ["38427914", "38427606", "38426620"]
    assert query_of(stub_http.requests[1])["id"] == ["38427914,38427606,38426620"]
    assert items[0]["url"] == "https://pubmed.ncbi.nlm.nih.gov/38427914/"
    assert items[0]["title"].startswith("Investigating genomic prediction")


def test_search_pubmed_skips_esummary_entirely_on_zero_hits(stub_http):
    stub_http.route(ESEARCH_PATH, fixture_bytes("pubmed_search_esearch_zero.json"))
    stub_http.route(ESUMMARY_PATH, fixture_bytes("pubmed_search_esummary.json"))

    assert ps.search_pubmed("nope[Title]", email=EMAIL, base_url=stub_http.url) == []
    assert [path_of(r) for r in stub_http.requests] == [ESEARCH_PATH]


def test_search_pubmed_caps_the_esummary_batch_at_max_results(stub_http):
    route_search_and_summary(stub_http)

    ps.search_pubmed("maize[Title]", email=EMAIL, max_results=2, base_url=stub_http.url)

    assert query_of(stub_http.requests[1])["id"] == ["38427914,38427606"]


def test_search_pubmed_propagates_an_esummary_failure(stub_http):
    stub_http.route(ESEARCH_PATH, fixture_bytes("pubmed_search_esearch.json"))
    stub_http.route(ESUMMARY_PATH, Response(body=b"", status=500))

    with pytest.raises(RuntimeError) as excinfo:
        ps.search_pubmed("maize[Title]", email=EMAIL, base_url=stub_http.url)

    assert "500" in str(excinfo.value)


def test_http_error_message_masks_the_api_key(stub_http):
    stub_http.route(ESEARCH_PATH, Response(body=b"nope", status=500))

    with pytest.raises(RuntimeError) as excinfo:
        ps.esearch_ids(
            "maize[Title]",
            email=EMAIL,
            base_url=stub_http.url,
            api_key="SUPERSECRETKEY123",
        )

    message = str(excinfo.value)
    # the caller logs what this module raises, so the key must not ride along
    assert "SUPERSECRETKEY123" not in message
    assert "api_key=REDACTED" in message
    # ... and the rest of the request is still there to debug with
    assert "term=maize" in message
    assert "500" in message


def test_trailing_slash_in_base_url_does_not_double_the_path(stub_http):
    # http.client collapses "//" on the wire, so the recorded path cannot show
    # this. The URL urllib was actually handed can: leave the route unregistered
    # and read it out of the 404 message.
    with pytest.raises(RuntimeError) as excinfo:
        ps.esearch_ids("maize[Title]", email=EMAIL, base_url=stub_http.url + "/")

    message = str(excinfo.value)
    assert f"{stub_http.url}/esearch.fcgi?" in message
    assert "//esearch.fcgi" not in message


def test_search_pubmed_works_with_a_trailing_slash_base_url(stub_http):
    route_search_and_summary(stub_http)

    items = ps.search_pubmed("maize[Title]", email=EMAIL, base_url=stub_http.url + "/")

    assert [i["external_id"] for i in items] == ["38427914", "38427606", "38426620"]


# ------------------------------------------------------------ seed rules ---


def test_module_imports_only_the_standard_library():
    """Rule 3: a seed imports stdlib and nothing else -- no brief.*, no deps."""
    tree = ast.parse(Path(ps.__file__).read_text(encoding="utf-8"))

    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                roots.add(f"<relative import level {node.level}>")
            elif node.module:
                roots.add(node.module.split(".")[0])

    assert roots == {"__future__", "datetime", "json", "re", "urllib"}


def test_module_imports_nothing_from_the_app():
    source = Path(ps.__file__).read_text(encoding="utf-8")

    assert "brief." not in source.replace("topic-brief", "")
    assert "import os" not in source
    assert "os.environ" not in source
