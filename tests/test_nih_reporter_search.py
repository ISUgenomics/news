"""Tests for the nih_reporter_search seed.

Every HTTP test runs against a real stub server on an ephemeral port
(``tests/harness/stub_http.py``). Nothing here touches the live RePORTER API,
and nothing here mocks ``urlopen`` — the assertions are about the bytes the
module actually put on a socket.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from brief.lib.nih_reporter_search import (
    MAX_OFFSET,
    MAX_PAGE_SIZE,
    MAX_RECORDS,
    NIHReporterError,
    build_criteria,
    fetch_projects,
    normalize_project,
    search_projects,
)
from harness.stub_http import Response, stub_http, unreachable_url  # noqa: F401

SEARCH_PATH = "/v2/projects/search"
FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "nih_reporter_search_page.json"
)


def fixture_page() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class Recorder:
    """A ``sleep`` that records instead of sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def page(results: list[dict], total: int | None = None) -> dict:
    body: dict = {"results": results}
    if total is not None:
        body["meta"] = {"total": total, "offset": 0, "limit": len(results)}
    return body


def tiny(n: int) -> list[dict]:
    return [{"appl_id": i, "project_num": f"P{i}"} for i in range(n)]


# --------------------------------------------------------- build_criteria ---


def test_build_criteria_org_names_and_start_window():
    assert build_criteria(
        org_names=["IOWA STATE UNIVERSITY"],
        start_from="2026-09-01",
        start_to="2026-09-08",
    ) == {
        "org_names": ["IOWA STATE UNIVERSITY"],
        "project_start_date": {"from_date": "2026-09-01", "to_date": "2026-09-08"},
    }


def test_build_criteria_text_search_uses_documented_defaults():
    criteria = build_criteria(advanced_text_search="protein folding")
    assert criteria == {
        "advanced_text_search": {
            "operator": "and",
            "search_field": "projecttitle,terms,abstracttext",
            "search_text": "protein folding",
        }
    }


def test_build_criteria_text_search_operator_and_field_are_parameters():
    criteria = build_criteria(
        advanced_text_search="crispr", operator="or", search_field="projecttitle"
    )
    assert criteria["advanced_text_search"] == {
        "operator": "or",
        "search_field": "projecttitle",
        "search_text": "crispr",
    }


def test_build_criteria_date_field_selects_award_notice_window():
    criteria = build_criteria(
        start_from="2026-01-01", start_to="2026-01-31", date_field="award_notice_date"
    )
    assert criteria == {
        "award_notice_date": {"from_date": "2026-01-01", "to_date": "2026-01-31"}
    }
    assert "project_start_date" not in criteria


def test_build_criteria_accepts_a_half_open_window():
    assert build_criteria(org_names=["X"], start_from="2026-01-01") == {
        "org_names": ["X"],
        "project_start_date": {"from_date": "2026-01-01"},
    }


def test_build_criteria_omits_empty_inputs():
    assert build_criteria(org_names=[], advanced_text_search="  ") == {}


def test_build_criteria_extra_criteria_merges_and_wins():
    criteria = build_criteria(
        org_names=["A"],
        extra_criteria={"fiscal_years": [2026], "org_names": ["B"]},
    )
    assert criteria == {"fiscal_years": [2026], "org_names": ["B"]}


def test_build_criteria_does_not_alias_caller_lists():
    orgs = ["A"]
    criteria = build_criteria(org_names=orgs)
    orgs.append("B")
    assert criteria["org_names"] == ["A"]


@pytest.mark.parametrize("bad", ["2026/09/01", "Sep 1 2026", "2026-9-1", ""])
def test_build_criteria_rejects_malformed_dates(bad):
    with pytest.raises(ValueError):
        build_criteria(start_from=bad)


def test_build_criteria_rejects_unknown_date_field():
    with pytest.raises(ValueError):
        build_criteria(start_from="2026-01-01", date_field="fiscal_year")


# --------------------------------------------------------- search_projects ---


def test_search_projects_posts_json_to_the_v2_endpoint(stub_http):
    stub_http.route(SEARCH_PATH, page(tiny(1), total=1))

    search_projects(
        {"org_names": ["X"]},
        base_url=stub_http.url,
        page_size=250,
        user_agent="topic-brief-test/9.9",
        sleep=Recorder(),
    )

    sent = stub_http.last(SEARCH_PATH)
    assert sent.method == "POST"
    assert sent.path == SEARCH_PATH
    assert sent.headers["Content-Type"] == "application/json"
    assert sent.headers["Accept"] == "application/json"
    assert sent.headers["User-Agent"] == "topic-brief-test/9.9"
    assert sent.json == {
        "criteria": {"org_names": ["X"]},
        "offset": 0,
        "limit": 250,
        "sort_field": "project_start_date",
        "sort_order": "desc",
    }


def test_search_projects_base_url_trailing_slash_does_not_double(stub_http):
    stub_http.route(SEARCH_PATH, page([], total=0))
    search_projects(
        {"org_names": ["X"]}, base_url=stub_http.url + "/", sleep=Recorder()
    )
    assert stub_http.last().path == SEARCH_PATH


def test_search_projects_walks_pages_until_total_is_exhausted(stub_http):
    def handler(request):
        offset = request.json["offset"]
        limit = request.json["limit"]
        return {
            "meta": {"total": 5},
            "results": [
                {"appl_id": offset + i, "project_num": f"P{offset + i}"}
                for i in range(min(limit, 5 - offset))
            ],
        }

    stub_http.route(SEARCH_PATH, handler)
    napped = Recorder()

    records = search_projects(
        {"org_names": ["X"]},
        base_url=stub_http.url,
        page_size=2,
        sleep=napped,
        page_delay_s=0.25,
    )

    assert [r["appl_id"] for r in records] == [0, 1, 2, 3, 4]
    assert [r.json["offset"] for r in stub_http.requests_to(SEARCH_PATH)] == [0, 2, 4]
    # slept between pages, never after the last one
    assert napped.calls == [0.25, 0.25]


def test_search_projects_stops_at_max_records_and_shrinks_the_last_limit(stub_http):
    stub_http.route(
        SEARCH_PATH, lambda r: {"meta": {"total": 99}, "results": tiny(r.json["limit"])}
    )
    napped = Recorder()

    records = search_projects(
        {"org_names": ["X"]},
        base_url=stub_http.url,
        page_size=2,
        max_records=3,
        sleep=napped,
    )

    assert len(records) == 3
    assert [r.json["limit"] for r in stub_http.requests_to(SEARCH_PATH)] == [2, 1]
    assert napped.calls == [1.0]


def test_search_projects_caps_page_size_at_500(stub_http):
    stub_http.route(SEARCH_PATH, page([], total=0))
    search_projects(
        {"org_names": ["X"]}, base_url=stub_http.url, page_size=5_000, sleep=Recorder()
    )
    assert stub_http.last().json["limit"] == MAX_PAGE_SIZE


def test_search_projects_clamps_page_size_to_at_least_one(stub_http):
    stub_http.route(SEARCH_PATH, page([], total=0))
    search_projects(
        {"org_names": ["X"]}, base_url=stub_http.url, page_size=0, sleep=Recorder()
    )
    assert stub_http.last().json["limit"] == 1


def test_search_projects_omits_sort_when_sort_field_is_none(stub_http):
    stub_http.route(SEARCH_PATH, page([], total=0))
    search_projects(
        {"org_names": ["X"]},
        base_url=stub_http.url,
        sort_field=None,
        sleep=Recorder(),
    )
    sent = stub_http.last(SEARCH_PATH).json
    assert "sort_field" not in sent
    assert "sort_order" not in sent


def test_search_projects_sort_order_is_sent_as_given(stub_http):
    stub_http.route(SEARCH_PATH, page([], total=0))
    search_projects(
        {"org_names": ["X"]},
        base_url=stub_http.url,
        sort_field="award_amount",
        sort_order="asc",
        sleep=Recorder(),
    )
    sent = stub_http.last(SEARCH_PATH).json
    assert sent["sort_field"] == "award_amount"
    assert sent["sort_order"] == "asc"


def test_search_projects_never_returns_more_than_max_records(stub_http):
    """A page that over-delivers does not push the result past the cap."""
    stub_http.route(
        SEARCH_PATH,
        lambda r: {"meta": {"total": 99}, "results": tiny(r.json["limit"] * 3)},
    )

    records = search_projects(
        {"org_names": ["X"]},
        base_url=stub_http.url,
        page_size=2,
        max_records=3,
        sleep=Recorder(),
    )

    assert len(records) == 3


def test_search_projects_with_max_records_zero_opens_no_socket(stub_http):
    stub_http.route(SEARCH_PATH, page(tiny(5), total=5))
    napped = Recorder()
    assert (
        search_projects(
            {"org_names": ["X"]},
            base_url=stub_http.url,
            max_records=0,
            sleep=napped,
        )
        == []
    )
    assert stub_http.requests == []
    assert napped.calls == []


def test_search_projects_never_asks_for_an_offset_past_14999(stub_http):
    stub_http.route(
        SEARCH_PATH,
        lambda r: {"meta": {"total": 999_999}, "results": tiny(r.json["limit"])},
    )

    records = search_projects(
        {"org_names": ["X"]},
        base_url=stub_http.url,
        max_records=1_000_000,
        sleep=Recorder(),
    )

    offsets = [r.json["offset"] for r in stub_http.requests_to(SEARCH_PATH)]
    assert len(records) == MAX_RECORDS
    assert max(offsets) <= MAX_OFFSET
    assert offsets[-1] == MAX_RECORDS - MAX_PAGE_SIZE


def test_search_projects_returns_empty_list_when_nothing_matched(stub_http):
    stub_http.route(SEARCH_PATH, page([], total=0))
    napped = Recorder()
    assert (
        search_projects({"org_names": ["X"]}, base_url=stub_http.url, sleep=napped)
        == []
    )
    assert napped.calls == []


def test_search_projects_stops_when_meta_is_absent_and_a_page_is_short(stub_http):
    stub_http.route(SEARCH_PATH, {"results": tiny(3)})
    records = search_projects(
        {"org_names": ["X"]}, base_url=stub_http.url, page_size=10, sleep=Recorder()
    )
    assert len(records) == 3
    assert len(stub_http.requests_to(SEARCH_PATH)) == 1


def test_search_projects_does_not_mutate_the_caller_criteria(stub_http):
    stub_http.route(SEARCH_PATH, page([], total=0))
    criteria = {"org_names": ["X"]}
    search_projects(criteria, base_url=stub_http.url, sleep=Recorder())
    assert criteria == {"org_names": ["X"]}


def test_search_projects_rejects_empty_criteria_without_opening_a_socket(stub_http):
    stub_http.route(SEARCH_PATH, page([], total=0))
    with pytest.raises(ValueError):
        search_projects({}, base_url=stub_http.url, sleep=Recorder())
    assert stub_http.requests == []


def test_search_projects_raises_on_non_2xx(stub_http):
    stub_http.route(SEARCH_PATH, Response(body={"error": "bad"}, status=500))
    with pytest.raises(NIHReporterError) as excinfo:
        search_projects({"org_names": ["X"]}, base_url=stub_http.url, sleep=Recorder())
    assert "500" in str(excinfo.value)


def test_search_projects_raises_on_a_connection_failure(unreachable_url):
    with pytest.raises(NIHReporterError):
        search_projects(
            {"org_names": ["X"]}, base_url=unreachable_url, sleep=Recorder()
        )


def test_search_projects_raises_on_timeout(stub_http):
    def slow(request):
        time.sleep(0.6)
        return page([], total=0)

    stub_http.route(SEARCH_PATH, slow)
    with pytest.raises(NIHReporterError):
        search_projects(
            {"org_names": ["X"]},
            base_url=stub_http.url,
            timeout_s=0.05,
            sleep=Recorder(),
        )


def test_search_projects_raises_on_malformed_json(stub_http):
    stub_http.route(SEARCH_PATH, b"<html>RePORTER is having a moment</html>")
    with pytest.raises(NIHReporterError):
        search_projects({"org_names": ["X"]}, base_url=stub_http.url, sleep=Recorder())


def test_search_projects_raises_when_the_results_key_is_missing(stub_http):
    stub_http.route(SEARCH_PATH, {"meta": {"total": 7}})
    with pytest.raises(NIHReporterError) as excinfo:
        search_projects({"org_names": ["X"]}, base_url=stub_http.url, sleep=Recorder())
    assert "results" in str(excinfo.value)


def test_search_projects_raises_when_results_is_not_a_list(stub_http):
    stub_http.route(SEARCH_PATH, {"meta": {"total": 1}, "results": {"appl_id": 1}})
    with pytest.raises(NIHReporterError):
        search_projects({"org_names": ["X"]}, base_url=stub_http.url, sleep=Recorder())


def test_search_projects_never_returns_a_silent_empty_list_on_failure(stub_http):
    """A failing page is an exception, not a truncated result."""
    state = {"n": 0}

    def flaky(request):
        state["n"] += 1
        if state["n"] == 1:
            return {"meta": {"total": 4}, "results": tiny(2)}
        return Response(body={"error": "boom"}, status=503)

    stub_http.route(SEARCH_PATH, flaky)
    with pytest.raises(NIHReporterError):
        search_projects(
            {"org_names": ["X"]}, base_url=stub_http.url, page_size=2, sleep=Recorder()
        )


# ------------------------------------------------------- normalize_project ---


def test_normalize_project_flattens_a_real_record():
    record = fixture_page()["results"][0]
    assert normalize_project(record) == {
        "external_id": "1R24GM154192-01",
        "appl_id": 10878415,
        "url": "https://reporter.nih.gov/project-details/10878415",
        "title": "NCCAT: National Center for CryoEM Access and Training",
        "body": "This resource provides open access to cryo-electron microscopy.",
        "published_at": "2024-08-15",
        "start_date": "2024-09-01",
        "end_date": "2029-08-31",
        "org_name": "IOWA STATE UNIVERSITY",
        "pi_names": ["Ana Muñoz", "Wei Zhang"],
        "agency": "National Institute of General Medical Sciences",
        "amount": 7709519,
        "fiscal_year": 2024,
        "raw": record,
    }


def test_normalize_project_keeps_the_raw_record_untouched():
    record = fixture_page()["results"][0]
    before = json.dumps(record, sort_keys=True)
    out = normalize_project(record)
    assert out["raw"] is record
    assert json.dumps(record, sort_keys=True) == before


def test_normalize_project_falls_back_to_project_start_date_for_published_at():
    record = fixture_page()["results"][1]
    out = normalize_project(record)
    assert out["published_at"] == "2023-04-01"
    assert out["body"] == ""


def test_normalize_project_preserves_unicode():
    record = fixture_page()["results"][1]
    out = normalize_project(record)
    assert out["title"] == "Machine learning for outbreak détection"


def test_normalize_project_builds_a_detail_url_from_appl_id():
    out = normalize_project({"appl_id": 42})
    assert out["url"] == "https://reporter.nih.gov/project-details/42"


def test_normalize_project_honors_a_custom_detail_url_base():
    out = normalize_project({"appl_id": 42}, detail_url_base="https://example.test/p/")
    assert out["url"] == "https://example.test/p/42"


def test_normalize_project_missing_fields_are_none_or_empty_never_keyerror():
    out = normalize_project({})
    assert out == {
        "external_id": None,
        "appl_id": None,
        "url": None,
        "title": None,
        "body": "",
        "published_at": None,
        "start_date": None,
        "end_date": None,
        "org_name": None,
        "pi_names": [],
        "agency": None,
        "amount": None,
        "fiscal_year": None,
        "raw": {},
    }


def test_normalize_project_survives_wrongly_typed_nested_fields():
    out = normalize_project(
        {
            "organization": "IOWA STATE",
            "agency_ic_admin": "NIGMS",
            "principal_investigators": [{"first_name": "No"}, "Ada Lovelace", None],
            "award_amount": None,
        }
    )
    assert out["org_name"] is None
    assert out["agency"] == "NIGMS"
    assert out["pi_names"] == []
    assert out["amount"] is None


def test_normalize_project_coerces_a_non_string_abstract_to_empty_body():
    assert normalize_project({"abstract_text": {"p": "html soup"}})["body"] == ""
    assert normalize_project({"abstract_text": 0})["body"] == ""


def test_normalize_project_rejects_a_non_dict_record():
    with pytest.raises(NIHReporterError):
        normalize_project(["not", "a", "record"])


# ---------------------------------------------------------- fetch_projects ---


def test_fetch_projects_sends_the_built_criteria_and_returns_normalized_dicts(
    stub_http,
):
    stub_http.route(SEARCH_PATH, fixture_page())

    items = fetch_projects(
        org_names=["IOWA STATE UNIVERSITY"],
        start_from="2026-09-01",
        start_to="2026-09-08",
        base_url=stub_http.url,
        sleep=Recorder(),
    )

    assert stub_http.last(SEARCH_PATH).json["criteria"] == {
        "org_names": ["IOWA STATE UNIVERSITY"],
        "project_start_date": {"from_date": "2026-09-01", "to_date": "2026-09-08"},
    }
    assert [i["external_id"] for i in items] == ["1R24GM154192-01", "5R01AI123456-02"]
    assert items[0]["raw"]["appl_id"] == 10878415


def test_fetch_projects_sorts_newest_first_whatever_order_the_api_used(stub_http):
    older, newer = fixture_page()["results"][1], fixture_page()["results"][0]
    stub_http.route(SEARCH_PATH, {"meta": {"total": 2}, "results": [older, newer]})

    items = fetch_projects(org_names=["X"], base_url=stub_http.url, sleep=Recorder())

    assert [i["start_date"] for i in items] == ["2024-09-01", "2023-04-01"]


def test_fetch_projects_sorts_by_the_chosen_date_field(stub_http):
    a = {
        "appl_id": 1,
        "project_start_date": "2020-01-01",
        "award_notice_date": "2026-05-05",
    }
    b = {
        "appl_id": 2,
        "project_start_date": "2026-01-01",
        "award_notice_date": "2024-05-05",
    }
    stub_http.route(SEARCH_PATH, {"meta": {"total": 2}, "results": [b, a]})

    items = fetch_projects(
        start_from="2024-01-01",
        date_field="award_notice_date",
        base_url=stub_http.url,
        sleep=Recorder(),
    )

    assert [i["appl_id"] for i in items] == [1, 2]
    assert stub_http.last(SEARCH_PATH).json["sort_field"] == "award_notice_date"


def test_fetch_projects_sorts_records_missing_the_chosen_date_last(stub_http):
    """No award_notice_date means last, not 'borrow the project start date'."""
    noticed = {
        "appl_id": 1,
        "project_start_date": "2019-01-01",
        "award_notice_date": "2024-05-05",
    }
    never_noticed = {
        "appl_id": 2,
        "project_start_date": "2030-01-01",
        "award_notice_date": None,
    }
    stub_http.route(
        SEARCH_PATH, {"meta": {"total": 2}, "results": [never_noticed, noticed]}
    )

    items = fetch_projects(
        start_from="2024-01-01",
        date_field="award_notice_date",
        base_url=stub_http.url,
        sleep=Recorder(),
    )

    assert [i["appl_id"] for i in items] == [1, 2]
    # published_at still falls back to the start date; only the order refuses to
    assert items[1]["published_at"] == "2030-01-01"


def test_fetch_projects_sends_a_unicode_query_intact(stub_http):
    stub_http.route(SEARCH_PATH, page([], total=0))

    fetch_projects(
        advanced_text_search="santé publique 日本",
        base_url=stub_http.url,
        sleep=Recorder(),
    )

    sent = stub_http.last(SEARCH_PATH)
    assert (
        sent.json["criteria"]["advanced_text_search"]["search_text"]
        == "santé publique 日本"
    )
    assert "santé publique 日本".encode("utf-8") in sent.body


def test_fetch_projects_returns_empty_list_for_no_matches(stub_http):
    stub_http.route(SEARCH_PATH, page([], total=0))
    assert (
        fetch_projects(
            org_names=["Nowhere U"], base_url=stub_http.url, sleep=Recorder()
        )
        == []
    )


def test_fetch_projects_propagates_reporter_errors(stub_http):
    stub_http.route(SEARCH_PATH, Response(body={"error": "nope"}, status=404))
    with pytest.raises(NIHReporterError):
        fetch_projects(org_names=["X"], base_url=stub_http.url, sleep=Recorder())


def test_fetch_projects_rejects_a_criteria_less_search(stub_http):
    stub_http.route(SEARCH_PATH, page([], total=0))
    with pytest.raises(ValueError):
        fetch_projects(base_url=stub_http.url, sleep=Recorder())
    assert stub_http.requests == []


def test_fetch_projects_passes_extra_criteria_through(stub_http):
    stub_http.route(SEARCH_PATH, page([], total=0))
    fetch_projects(
        extra_criteria={
            "org_names_exact_match": ["IOWA STATE UNIVERSITY"],
            "fiscal_years": [2026],
        },
        base_url=stub_http.url,
        sleep=Recorder(),
    )
    assert stub_http.last(SEARCH_PATH).json["criteria"] == {
        "org_names_exact_match": ["IOWA STATE UNIVERSITY"],
        "fiscal_years": [2026],
    }
