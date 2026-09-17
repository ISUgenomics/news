"""Tests for the nsf_award_search seed.

Every HTTP path goes through the real stub server in tests/harness/stub_http.py:
a real socket, a real urllib client, real bytes on the wire. Nothing here talks
to api.nsf.gov — the one saved response in tests/fixtures/ was captured from it
by hand and is replayed by the stub.
"""

from __future__ import annotations

import datetime
import json
import os
import time
import urllib.parse
from pathlib import Path

import pytest

from brief.lib.nsf_award_search import (
    DEFAULT_BASE_URL,
    NSFSearchError,
    build_query_url,
    normalize_award,
    search_nsf_awards,
)
from harness.stub_http import Response, stub_http, unreachable_url  # noqa: F401

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PAGE = json.loads((FIXTURES / "nsf_award_search_page.json").read_text("utf-8"))

JAN1 = datetime.date(2026, 1, 1)
MAR31 = datetime.date(2026, 3, 31)

PRINT_FIELDS_EXPECTED = (
    "id,title,date,startDate,abstractText,piFirstName,piLastName,"
    "pdPIName,awardeeName,fundsObligatedAmt,agency,program,primaryProgram"
)


class _PathOnlyRoutes(dict):
    """Route lookup that ignores the query string.

    The vendored harness matches a route against the whole request path, and
    every request this module makes carries a query string, so a route
    registered as "/awards.json" would never match. Subclassing the routes
    dict keeps the real server, the real socket and the recorded full paths
    (which the assertions below read) exactly as they are — only the lookup
    is relaxed. Nothing is mocked.
    """

    @staticmethod
    def _bare(key):
        method, path = key
        return (method, path.split("?", 1)[0])

    def __contains__(self, key):
        return super().__contains__(self._bare(key))

    def __getitem__(self, key):
        return super().__getitem__(self._bare(key))


@pytest.fixture
def nsf_stub(stub_http):
    stub_http.routes = _PathOnlyRoutes(stub_http.routes)
    return stub_http


@pytest.fixture
def process_timezone():
    """Set the real process timezone, and put it back afterwards.

    TZ + tzset() is how a process picks a zone; nothing is patched, and the
    restore is manual rather than via monkeypatch because monkeypatch undoes
    the environment variable *after* this fixture's teardown, which would
    leave the C library's cached zone pointing at the test's value for the
    rest of the session.
    """
    original = os.environ.get("TZ")

    def _set(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    yield _set
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


def query_of(request) -> dict[str, list[str]]:
    """The query string the module actually sent, parsed."""
    return urllib.parse.parse_qs(urllib.parse.urlparse(request.path).query)


def award(n: int) -> dict:
    return {
        "id": str(1000 + n),
        "title": f"Award {n}",
        "abstractText": f"Body {n}",
        "date": "02/03/2026",
        "piFirstName": "Ada",
        "piLastName": "Lovelace",
        "awardeeName": "Example State University",
        "fundsObligatedAmt": "12345",
        "agency": "NSF",
        "program": "Some Program",
    }


def page_of(awards: list[dict]) -> dict:
    return {"response": {"award": awards, "metadata": {"totalCount": len(awards)}}}


# --------------------------------------------------------- build_query_url ---


def test_build_query_url_is_pinned_exactly():
    url = build_query_url(
        DEFAULT_BASE_URL,
        awardee="Example State University",
        keyword="quantum sensing",
        pi_name="Ada Lovelace",
        date_start=JAN1,
        date_end=MAR31,
    )
    assert url == (
        "https://api.nsf.gov/services/v1/awards.json"
        "?keyword=quantum+sensing"
        "&awardeeName=Example+State+University"
        "&pdPIName=Ada+Lovelace"
        "&dateStart=01%2F01%2F2026"
        "&dateEnd=03%2F31%2F2026"
        "&offset=0"
        "&rpp=25"
        "&printFields=" + urllib.parse.quote_plus(PRINT_FIELDS_EXPECTED)
    )


def test_build_query_url_omits_filters_that_were_not_given():
    url = build_query_url(
        DEFAULT_BASE_URL,
        awardee=None,
        keyword="cryo-em",
        pi_name=None,
        date_start=JAN1,
        date_end=MAR31,
    )
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert params["keyword"] == ["cryo-em"]
    assert "awardeeName" not in params
    assert "pdPIName" not in params


def test_build_query_url_uses_nsf_mm_dd_yyyy_dates():
    url = build_query_url(
        DEFAULT_BASE_URL,
        awardee=None,
        keyword="x",
        pi_name=None,
        date_start=datetime.date(2026, 7, 4),
        date_end=datetime.date(2026, 12, 25),
    )
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert params["dateStart"] == ["07/04/2026"]
    assert params["dateEnd"] == ["12/25/2026"]


def test_build_query_url_percent_encodes_unicode_and_punctuation():
    url = build_query_url(
        DEFAULT_BASE_URL,
        awardee="Universidad de Córdoba",
        keyword='"machine learning" AND soil*',
        pi_name=None,
        date_start=JAN1,
        date_end=MAR31,
    )
    assert "Córdoba" not in url  # must be escaped on the wire
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert params["awardeeName"] == ["Universidad de Córdoba"]
    assert params["keyword"] == ['"machine learning" AND soil*']


def test_build_query_url_offset_and_rpp_are_explicit():
    url = build_query_url(
        DEFAULT_BASE_URL,
        awardee="X",
        keyword=None,
        pi_name=None,
        date_start=JAN1,
        date_end=MAR31,
        offset=50,
        rpp=25,
    )
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert params["offset"] == ["50"]
    assert params["rpp"] == ["25"]


def test_build_query_url_appends_to_a_base_url_that_already_has_a_query():
    url = build_query_url(
        DEFAULT_BASE_URL + "?callback=cb",
        awardee="X",
        keyword=None,
        pi_name=None,
        date_start=JAN1,
        date_end=MAR31,
    )
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert params["callback"] == ["cb"]
    assert params["awardeeName"] == ["X"]


# --------------------------------------------------------- normalize_award ---


def test_normalize_award_maps_a_real_record():
    got = normalize_award(PAGE["response"]["award"][0])
    assert got["external_id"] == "2500122"
    assert got["url"] == ("https://www.nsf.gov/awardsearch/showAward?AWD_ID=2500122")
    assert got["title"].startswith("Research Initiation Award:")
    assert got["body"].startswith("The Historically Black Colleges")
    assert got["published_at"] == "2026-08-13"
    assert got["pi_name"] == "Alex Doe"
    assert got["awardee"] == "Tuskegee University"
    assert got["amount"] == "450000"
    assert got["agency"] == "NSF"
    assert got["program"].startswith("Biotechnology")


def test_normalize_award_tolerates_a_completely_empty_record():
    got = normalize_award({})
    assert set(got) == {
        "external_id",
        "url",
        "title",
        "body",
        "published_at",
        "pi_name",
        "awardee",
        "amount",
        "agency",
        "program",
        "raw",
    }
    assert got["external_id"] == ""
    assert got["url"] == ""
    assert got["title"] == ""
    assert got["body"] == ""
    assert got["published_at"] is None
    assert got["pi_name"] == ""
    assert got["amount"] == ""


def test_normalize_award_returns_none_for_an_unparseable_date():
    assert normalize_award({"id": "1", "date": "not a date"})["published_at"] is None
    assert normalize_award({"id": "1", "date": "2026-08-13"})["published_at"] is None
    assert normalize_award({"id": "1", "date": None})["published_at"] is None


def test_normalize_award_never_converts_the_amount():
    assert normalize_award({"fundsObligatedAmt": "0450000"})["amount"] == "0450000"
    # a JSON number passes through as the number it was, not as a string
    assert normalize_award({"fundsObligatedAmt": 450000})["amount"] == 450000


def test_normalize_award_keeps_the_raw_record_untouched():
    raw = dict(PAGE["response"]["award"][1])
    got = normalize_award(raw)
    assert got["raw"] == raw
    assert got["raw"] is raw


def test_normalize_award_builds_pi_name_from_whichever_half_exists():
    assert normalize_award({"piFirstName": "Ada"})["pi_name"] == "Ada"
    assert normalize_award({"piLastName": "Lovelace"})["pi_name"] == "Lovelace"
    assert (
        normalize_award({"piFirstName": "Ada", "piLastName": "Lovelace"})["pi_name"]
        == "Ada Lovelace"
    )


# ----------------------------------------------------- what the module SENT ---


def test_search_sends_a_get_with_the_user_agent_and_json_accept(nsf_stub):
    nsf_stub.route("/awards.json", page_of([]))
    search_nsf_awards(
        awardee="Example State University",
        date_start=JAN1,
        date_end=MAR31,
        base_url=nsf_stub.url + "/awards.json",
        user_agent="topic-brief/9.9",
    )
    request = nsf_stub.last()
    assert request.method == "GET"
    assert request.headers["User-Agent"] == "topic-brief/9.9"
    assert "json" in request.headers["Accept"]
    assert request.body == b""
    params = query_of(request)
    assert params["awardeeName"] == ["Example State University"]
    assert params["dateStart"] == ["01/01/2026"]
    assert params["dateEnd"] == ["03/31/2026"]
    assert params["offset"] == ["0"]
    assert params["rpp"] == ["25"]
    assert params["printFields"] == [PRINT_FIELDS_EXPECTED]


def test_search_starts_the_walk_at_offset_zero(nsf_stub):
    """offset is a 0-based record index; starting at 1 drops the first award."""
    nsf_stub.route("/awards.json", page_of([award(1)]))
    search_nsf_awards(
        keyword="x",
        date_start=JAN1,
        date_end=MAR31,
        base_url=nsf_stub.url + "/awards.json",
    )
    assert query_of(nsf_stub.last())["offset"] == ["0"]


def test_search_uses_today_for_an_open_ended_range(nsf_stub):
    nsf_stub.route("/awards.json", page_of([]))
    search_nsf_awards(
        keyword="x",
        date_start=JAN1,
        today=datetime.date(2026, 6, 15),
        base_url=nsf_stub.url + "/awards.json",
    )
    assert query_of(nsf_stub.last())["dateEnd"] == ["06/15/2026"]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs a POSIX timezone")
@pytest.mark.parametrize("tz", ["Pacific/Kiritimati", "Pacific/Pago_Pago"])
def test_search_falls_back_to_the_utc_clock_only_when_today_is_absent(
    nsf_stub, process_timezone, tz
):
    """The fallback is the UTC date, per the boundary, not the local one.

    Run twice under a real process timezone — UTC+14 and UTC-11 — because on
    a machine whose local date happens to equal UTC's, a local-clock
    implementation passes a naive assertion. At any instant at least one of
    these two zones is on a different calendar day from UTC, so
    datetime.date.today() cannot survive both runs. Nothing is patched: TZ
    plus tzset() is how a process actually chooses a timezone, and the two
    dates are read either side of the call so a rollover mid-test cannot
    flake it.
    """
    process_timezone(tz)
    nsf_stub.route("/awards.json", page_of([]))
    before = datetime.datetime.now(datetime.timezone.utc).date()
    search_nsf_awards(
        keyword="x", date_start=JAN1, base_url=nsf_stub.url + "/awards.json"
    )
    after = datetime.datetime.now(datetime.timezone.utc).date()
    sent = query_of(nsf_stub.last())["dateEnd"][0]
    assert sent in {before.strftime("%m/%d/%Y"), after.strftime("%m/%d/%Y")}


def test_search_ignores_today_when_date_end_was_given(nsf_stub):
    nsf_stub.route("/awards.json", page_of([]))
    search_nsf_awards(
        keyword="x",
        date_start=JAN1,
        date_end=MAR31,
        today=datetime.date(2026, 6, 15),
        base_url=nsf_stub.url + "/awards.json",
    )
    assert query_of(nsf_stub.last())["dateEnd"] == ["03/31/2026"]


# ------------------------------------------------------------ what it RETURNS ---


def test_search_returns_normalized_awards_from_a_saved_response(nsf_stub):
    nsf_stub.route("/awards.json", PAGE)
    got = search_nsf_awards(
        awardee="Tuskegee University",
        date_start=JAN1,
        date_end=MAR31,
        base_url=nsf_stub.url + "/awards.json",
    )
    assert [a["external_id"] for a in got] == ["2500122", "2619699"]
    assert got[0]["pi_name"] == "Alex Doe"
    assert got[1]["amount"] == "399174"
    assert got[0]["raw"]["awardeeName"] == "Tuskegee University"


def test_search_returns_an_empty_list_when_nothing_matches(nsf_stub):
    nsf_stub.route("/awards.json", page_of([]))
    got = search_nsf_awards(
        keyword="nothing matches this",
        date_start=JAN1,
        date_end=MAR31,
        base_url=nsf_stub.url + "/awards.json",
    )
    assert got == []
    assert len(nsf_stub.requests) == 1


def test_search_preserves_unicode_through_the_socket(nsf_stub):
    record = award(1) | {
        "title": "Étude — sensing in Córdoba",
        "abstractText": "naïve résumé — 日本語",
        "awardeeName": "Universidad de Córdoba",
    }
    # raw UTF-8 bytes on the wire, not JSON's \uXXXX escapes: the escapes are
    # pure ASCII and would survive any decoding, so they cannot catch a wrong
    # charset. These bytes can.
    body = json.dumps(page_of([record]), ensure_ascii=False).encode("utf-8")
    assert b"\xc3\x89tude" in body
    nsf_stub.route("/awards.json", body)
    got = search_nsf_awards(
        keyword="x",
        date_start=JAN1,
        date_end=MAR31,
        base_url=nsf_stub.url + "/awards.json",
    )
    assert got[0]["title"] == "Étude — sensing in Córdoba"
    assert got[0]["body"] == "naïve résumé — 日本語"
    assert got[0]["awardee"] == "Universidad de Córdoba"


# ---------------------------------------------------------------- pagination ---


def test_search_walks_pages_until_a_short_one(nsf_stub):
    awards = [award(n) for n in range(60)]

    def handler(request):
        offset = int(query_of(request)["offset"][0])
        return page_of(awards[offset : offset + 25])

    nsf_stub.route("/awards.json", handler)
    got = search_nsf_awards(
        keyword="x",
        date_start=JAN1,
        date_end=MAR31,
        base_url=nsf_stub.url + "/awards.json",
    )
    assert [a["external_id"] for a in got] == [a["id"] for a in awards]
    assert [query_of(r)["offset"][0] for r in nsf_stub.requests] == ["0", "25", "50"]


def test_search_stops_on_an_exactly_full_last_page_plus_an_empty_one(nsf_stub):
    awards = [award(n) for n in range(25)]

    def handler(request):
        offset = int(query_of(request)["offset"][0])
        return page_of(awards[offset : offset + 25])

    nsf_stub.route("/awards.json", handler)
    got = search_nsf_awards(
        keyword="x",
        date_start=JAN1,
        date_end=MAR31,
        base_url=nsf_stub.url + "/awards.json",
    )
    assert len(got) == 25
    assert len(nsf_stub.requests) == 2


def test_search_stops_at_max_pages(nsf_stub):
    def handler(request):
        offset = int(query_of(request)["offset"][0])
        return page_of([award(offset + n) for n in range(25)])

    nsf_stub.route("/awards.json", handler)
    got = search_nsf_awards(
        keyword="x",
        date_start=JAN1,
        date_end=MAR31,
        base_url=nsf_stub.url + "/awards.json",
        max_pages=2,
    )
    assert len(got) == 50
    assert len(nsf_stub.requests) == 2


# ------------------------------------------------------------ bad arguments ---


def test_search_requires_at_least_one_filter(nsf_stub):
    nsf_stub.route("/awards.json", page_of([]))
    with pytest.raises(ValueError, match="awardee|keyword|pi_name"):
        search_nsf_awards(
            date_start=JAN1,
            date_end=MAR31,
            base_url=nsf_stub.url + "/awards.json",
        )
    assert nsf_stub.requests == []


def test_search_treats_a_blank_filter_as_no_filter(nsf_stub):
    nsf_stub.route("/awards.json", page_of([]))
    with pytest.raises(ValueError):
        search_nsf_awards(
            awardee="   ",
            keyword="",
            date_start=JAN1,
            date_end=MAR31,
            base_url=nsf_stub.url + "/awards.json",
        )
    assert nsf_stub.requests == []


def test_search_rejects_an_inverted_date_range(nsf_stub):
    nsf_stub.route("/awards.json", page_of([]))
    with pytest.raises(ValueError, match="date_end"):
        search_nsf_awards(
            keyword="x",
            date_start=MAR31,
            date_end=JAN1,
            base_url=nsf_stub.url + "/awards.json",
        )
    assert nsf_stub.requests == []


def test_search_rejects_a_max_pages_that_would_fetch_nothing(nsf_stub):
    nsf_stub.route("/awards.json", page_of([]))
    with pytest.raises(ValueError, match="max_pages"):
        search_nsf_awards(
            keyword="x",
            date_start=JAN1,
            date_end=MAR31,
            base_url=nsf_stub.url + "/awards.json",
            max_pages=0,
        )
    assert nsf_stub.requests == []


# ------------------------------------------------------------- failure paths ---


def test_search_raises_on_a_non_200(nsf_stub):
    nsf_stub.route("/awards.json", Response(body={"nope": True}, status=500))
    with pytest.raises(NSFSearchError, match="500"):
        search_nsf_awards(
            keyword="x",
            date_start=JAN1,
            date_end=MAR31,
            base_url=nsf_stub.url + "/awards.json",
        )


def test_search_raises_on_a_404_from_a_wrong_base_url(nsf_stub):
    with pytest.raises(NSFSearchError, match="404"):
        search_nsf_awards(
            keyword="x",
            date_start=JAN1,
            date_end=MAR31,
            base_url=nsf_stub.url + "/no/such/path.json",
        )


def test_search_raises_when_nothing_is_listening(unreachable_url):
    with pytest.raises(NSFSearchError):
        search_nsf_awards(
            keyword="x", date_start=JAN1, date_end=MAR31, base_url=unreachable_url
        )


def test_search_raises_when_the_server_is_too_slow(nsf_stub):
    def slow(request):
        time.sleep(1.0)
        return page_of([])

    nsf_stub.route("/awards.json", slow)
    started = time.monotonic()
    with pytest.raises(NSFSearchError):
        search_nsf_awards(
            keyword="x",
            date_start=JAN1,
            date_end=MAR31,
            base_url=nsf_stub.url + "/awards.json",
            timeout_s=0.2,
        )
    assert time.monotonic() - started < 1.0  # the timeout was honoured


def test_search_raises_on_a_body_that_is_not_json(nsf_stub):
    nsf_stub.route("/awards.json", b"<html>NSF is having a day</html>")
    with pytest.raises(NSFSearchError, match="JSON"):
        search_nsf_awards(
            keyword="x",
            date_start=JAN1,
            date_end=MAR31,
            base_url=nsf_stub.url + "/awards.json",
        )


def test_search_raises_on_json_that_is_not_an_award_envelope(nsf_stub):
    nsf_stub.route("/awards.json", {"something": "else"})
    with pytest.raises(NSFSearchError, match="response"):
        search_nsf_awards(
            keyword="x",
            date_start=JAN1,
            date_end=MAR31,
            base_url=nsf_stub.url + "/awards.json",
        )


def test_search_raises_when_the_envelope_has_no_award_list(nsf_stub):
    nsf_stub.route("/awards.json", {"response": {"metadata": {"totalCount": 3}}})
    with pytest.raises(NSFSearchError, match="award"):
        search_nsf_awards(
            keyword="x",
            date_start=JAN1,
            date_end=MAR31,
            base_url=nsf_stub.url + "/awards.json",
        )


def test_search_raises_on_an_award_list_of_the_wrong_type(nsf_stub):
    nsf_stub.route("/awards.json", {"response": {"award": {"id": "1"}}})
    with pytest.raises(NSFSearchError, match="award"):
        search_nsf_awards(
            keyword="x",
            date_start=JAN1,
            date_end=MAR31,
            base_url=nsf_stub.url + "/awards.json",
        )


def test_search_raises_on_a_service_notification_error_payload(nsf_stub):
    nsf_stub.route(
        "/awards.json",
        {
            "response": {
                "serviceNotification": [
                    {
                        "notificationType": "ERROR",
                        "notificationCode": "1",
                        "notificationMessage": "Invalid parameter: dateStart",
                    }
                ]
            }
        },
    )
    with pytest.raises(NSFSearchError, match="Invalid parameter: dateStart"):
        search_nsf_awards(
            keyword="x",
            date_start=JAN1,
            date_end=MAR31,
            base_url=nsf_stub.url + "/awards.json",
        )


def test_search_raises_on_the_second_page_rather_than_returning_a_partial(nsf_stub):
    def handler(request):
        if query_of(request)["offset"][0] == "0":
            return page_of([award(n) for n in range(25)])
        return Response(body={"boom": True}, status=503)

    nsf_stub.route("/awards.json", handler)
    with pytest.raises(NSFSearchError, match="503"):
        search_nsf_awards(
            keyword="x",
            date_start=JAN1,
            date_end=MAR31,
            base_url=nsf_stub.url + "/awards.json",
        )


def test_search_raises_on_a_non_object_inside_the_award_list(nsf_stub):
    """A list of strings must not reach normalize_award and become garbage."""
    nsf_stub.route("/awards.json", {"response": {"award": ["2500122", None]}})
    with pytest.raises(NSFSearchError, match="non-object"):
        search_nsf_awards(
            keyword="x",
            date_start=JAN1,
            date_end=MAR31,
            base_url=nsf_stub.url + "/awards.json",
        )


def test_search_raises_on_a_service_notification_of_any_type(nsf_stub):
    """The fail-closed decision, pinned.

    NSF's documented notificationType values include INFO and WARNING, and
    this module treats every one of them as fatal rather than returning the
    awards that came with it. That is a deliberate choice — a search that
    quietly returns fewer awards than exist is indistinguishable from a quiet
    week — so it gets a test, not a comment. The payload below carries a full
    page of perfectly good awards alongside the notice; they are still not
    returned.
    """
    nsf_stub.route(
        "/awards.json",
        {
            "response": {
                "award": [award(n) for n in range(3)],
                "serviceNotification": [
                    {"notificationType": "INFO", "notificationMessage": "partial data"}
                ],
            }
        },
    )
    with pytest.raises(NSFSearchError, match="partial data"):
        search_nsf_awards(
            keyword="x",
            date_start=JAN1,
            date_end=MAR31,
            base_url=nsf_stub.url + "/awards.json",
        )


def test_search_still_names_the_error_when_the_notification_has_no_message(nsf_stub):
    nsf_stub.route(
        "/awards.json",
        {"response": {"award": [], "serviceNotification": [{"notificationCode": "9"}]}},
    )
    with pytest.raises(NSFSearchError, match="no message given"):
        search_nsf_awards(
            keyword="x",
            date_start=JAN1,
            date_end=MAR31,
            base_url=nsf_stub.url + "/awards.json",
        )


def test_search_ignores_an_empty_service_notification_list(nsf_stub):
    """An empty list is not a notification. Awards still come back."""
    nsf_stub.route(
        "/awards.json",
        {"response": {"award": [award(1)], "serviceNotification": []}},
    )
    got = search_nsf_awards(
        keyword="x",
        date_start=JAN1,
        date_end=MAR31,
        base_url=nsf_stub.url + "/awards.json",
    )
    assert [a["external_id"] for a in got] == ["1001"]


# ------------------------------------------------------------ base_url guard ---


@pytest.mark.parametrize("scheme_url", ["ftp://example.invalid/awards.json", "/tmp/x"])
def test_search_rejects_a_base_url_that_is_not_http(scheme_url):
    with pytest.raises(ValueError, match="http"):
        search_nsf_awards(
            keyword="x", date_start=JAN1, date_end=MAR31, base_url=scheme_url
        )


def test_search_will_not_read_a_local_file_through_a_file_url(tmp_path):
    """urllib.request opens file:// happily; a base_url from a config file
    must not turn this seed into a local-file reader. Written as a real file
    on disk, so the test fails loudly if the guard is removed — the call would
    then succeed and return the planted award.
    """
    planted = tmp_path / "awards.json"
    planted.write_text(
        json.dumps(page_of([award(1)])),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="http"):
        search_nsf_awards(
            keyword="x",
            date_start=JAN1,
            date_end=MAR31,
            base_url=planted.as_uri(),
        )


# ------------------------------------------------- normalize_award tolerance ---


def test_normalize_award_coerces_fields_that_are_not_strings(nsf_stub):
    """"Tolerant of an unexpected type" is in the docstring, so it is pinned.

    NSF sends `id` as a string today, but `primaryProgram` is a list while
    `program` is a string, and the shape has changed before. Every text field
    must still be a str afterwards or a caller that does .strip() on a title
    blows up deep inside the brief, far from here.
    """
    got = normalize_award(
        {
            "id": 2500122,
            "title": 42,
            "abstractText": ["a", "b"],
            "awardeeName": {"name": "X"},
            "agency": None,
        }
    )
    assert got["external_id"] == "2500122"
    assert got["url"].endswith("AWD_ID=2500122")
    assert got["title"] == "42"
    assert isinstance(got["body"], str)
    assert isinstance(got["awardee"], str)
    assert got["agency"] == ""
    assert all(isinstance(got[k], str) for k in ("external_id", "url", "title", "body"))


def test_normalize_award_ignores_whitespace_around_a_date():
    assert normalize_award({"date": " 02/03/2026 "})["published_at"] == "2026-02-03"


# --------------------------------------------------- pagination, hostile page ---


def test_search_advances_by_the_records_received_not_a_fixed_page(nsf_stub):
    """A server that ignores rpp must not make the walk repeat awards.

    Stepping by a hard-coded 25 against 30-record pages re-requests records
    25-29 on every page, and the caller gets duplicates it never asked to
    dedup (the boundary says this seed does not dedup). Stepping by what
    arrived is correct for both a service that honours rpp and one that does
    not.
    """
    awards = [award(n) for n in range(70)]

    def handler(request):
        offset = int(query_of(request)["offset"][0])
        return page_of(awards[offset : offset + 30])  # 30 > rpp=25

    nsf_stub.route("/awards.json", handler)
    got = search_nsf_awards(
        keyword="x",
        date_start=JAN1,
        date_end=MAR31,
        base_url=nsf_stub.url + "/awards.json",
    )
    assert [a["external_id"] for a in got] == [a["id"] for a in awards]
    assert len(got) == len(set(a["external_id"] for a in got))
    assert [query_of(r)["offset"][0] for r in nsf_stub.requests] == ["0", "30", "60"]


def test_build_query_url_drops_a_blank_filter_that_sits_beside_a_real_one():
    """The blank-filter rule, pinned where it actually bites.

    search_nsf_awards() rejects a call whose filters are ALL blank, so that
    ValueError never exercises this line. The dangerous case is a profile that
    sets keyword and leaves awardee as an empty box: a blank that survives
    into the query is not ignored by NSF, it is a filter matching nothing, and
    the caller gets a confident empty list instead of an error. Both flavours
    of blank — "" and whitespace — must vanish, and the real filter must stay.
    """
    url = build_query_url(
        DEFAULT_BASE_URL,
        awardee="   ",
        keyword="cryo-em",
        pi_name="",
        date_start=JAN1,
        date_end=MAR31,
    )
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query, keep_blank_values=True)
    assert params["keyword"] == ["cryo-em"]
    assert "awardeeName" not in params
    assert "pdPIName" not in params


def test_search_does_not_send_a_blank_filter_over_the_wire(nsf_stub):
    nsf_stub.route("/awards.json", page_of([]))
    search_nsf_awards(
        keyword="cryo-em",
        awardee=" ",
        pi_name="",
        date_start=JAN1,
        date_end=MAR31,
        base_url=nsf_stub.url + "/awards.json",
    )
    sent = query_of(nsf_stub.last())
    assert sent["keyword"] == ["cryo-em"]
    assert "awardeeName" not in sent and "pdPIName" not in sent
