"""Tests for the usaspending_award_search seed.

Every network test runs against ``stub_http``, a real HTTP server on a loopback
port. Nothing here touches api.usaspending.gov. The fixtures under
``tests/fixtures/usaspending_award_search_*.json`` are real responses from the
live API with the long descriptions trimmed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from brief.lib.usaspending_award_search import (
    AWARD_TYPE_GROUPS,
    DEFAULT_FIELDS,
    DEFAULT_SORT,
    SEARCH_PATH,
    UsaspendingError,
    build_request,
    normalize_award,
    search_awards,
)
from harness.stub_http import Response, stub_http, unreachable_url  # noqa: F401

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(case: str) -> dict:
    return json.loads(
        (FIXTURES / f"usaspending_award_search_{case}.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def page1() -> dict:
    return load("grants_page1")


@pytest.fixture
def page2() -> dict:
    return load("grants_page2")


# --------------------------------------------------------------- constants ---


def test_award_type_groups_match_the_groups_the_api_enforces():
    """The 422 body the live API returns lists its groups; we mirror them."""
    live = load("mixed_group_422")["award_type_groups"]
    assert set(AWARD_TYPE_GROUPS) == set(live)
    for group, codes in live.items():
        assert AWARD_TYPE_GROUPS[group] == tuple(codes)


# ------------------------------------------------------------ build_request ---


def test_build_request_golden_body():
    """The whole body is pinned: field names, fields list, paging, sort."""
    assert build_request(
        start_date="2025-01-01",
        end_date="2025-03-31",
        award_type_codes=("02", "03"),
        recipient="Iowa State University",
        page=2,
        limit=50,
    ) == {
        "filters": {
            "time_period": [
                {
                    "start_date": "2025-01-01",
                    "end_date": "2025-03-31",
                    "date_type": "action_date",
                }
            ],
            "award_type_codes": ["02", "03"],
            "recipient_search_text": ["Iowa State University"],
        },
        "fields": list(DEFAULT_FIELDS),
        "page": 2,
        "limit": 50,
        "sort": "Award Amount",
        "order": "desc",
        "subawards": False,
    }


def test_build_request_omits_recipient_and_keywords_when_absent():
    filters = build_request(
        start_date="2025-01-01", end_date="2025-01-31", award_type_codes=["A"]
    )["filters"]
    assert "recipient_search_text" not in filters
    assert "keywords" not in filters


def test_build_request_carries_keywords():
    filters = build_request(
        start_date="2025-01-01",
        end_date="2025-01-31",
        award_type_codes=["A"],
        keywords=["soybean", "carbon capture"],
    )["filters"]
    assert filters["keywords"] == ["soybean", "carbon capture"]


def test_build_request_drops_an_empty_keyword_list():
    filters = build_request(
        start_date="2025-01-01",
        end_date="2025-01-31",
        award_type_codes=["A"],
        keywords=[],
    )["filters"]
    assert "keywords" not in filters


def test_build_request_honours_date_type():
    period = build_request(
        start_date="2025-01-01",
        end_date="2025-01-31",
        award_type_codes=["A"],
        date_type="new_awards_only",
    )["filters"]["time_period"][0]
    assert period["date_type"] == "new_awards_only"


def test_build_request_rejects_an_unknown_date_type():
    with pytest.raises(ValueError, match="date_type"):
        build_request(
            start_date="2025-01-01",
            end_date="2025-01-31",
            award_type_codes=["A"],
            date_type="whenever",
        )


def test_build_request_rejects_empty_award_type_codes():
    with pytest.raises(ValueError, match="award_type_codes"):
        build_request(
            start_date="2025-01-01", end_date="2025-01-31", award_type_codes=[]
        )


@pytest.mark.parametrize("limit", [0, 101])
def test_build_request_rejects_a_limit_outside_the_api_range(limit):
    with pytest.raises(ValueError, match="limit"):
        build_request(
            start_date="2025-01-01",
            end_date="2025-01-31",
            award_type_codes=["A"],
            limit=limit,
        )


def test_build_request_accepts_an_explicit_fields_list():
    body = build_request(
        start_date="2025-01-01",
        end_date="2025-01-31",
        award_type_codes=["A"],
        fields=["Award ID"],
    )
    assert body["fields"] == ["Award ID"]


def test_build_request_rejects_an_empty_fields_list():
    """The API requires at least one field; an empty list is a certain 422."""
    with pytest.raises(ValueError, match="fields"):
        build_request(
            start_date="2025-01-01",
            end_date="2025-01-31",
            award_type_codes=["A"],
            fields=[],
        )


def test_build_request_rejects_a_page_below_one():
    with pytest.raises(ValueError, match="page"):
        build_request(
            start_date="2025-01-01",
            end_date="2025-01-31",
            award_type_codes=["A"],
            page=0,
        )


def test_build_request_accepts_an_explicit_sort():
    """sort is validated per group, so the other four groups need their own."""
    body = build_request(
        start_date="2025-01-01",
        end_date="2025-01-31",
        award_type_codes=["07"],
        sort="Loan Value",
    )
    assert body["sort"] == "Loan Value"
    assert build_request(
        start_date="2025-01-01", end_date="2025-01-31", award_type_codes=["A"]
    )["sort"] == DEFAULT_SORT


# ---------------------------------------------------------- normalize_award ---


def test_normalize_award_maps_every_stable_key(page1):
    record = page1["results"][0]
    out = normalize_award(record, award_type_group="grants")
    assert out["external_id"] == record["Award ID"]
    assert out["internal_id"] == record["generated_internal_id"]
    assert out["url"] == (
        "https://www.usaspending.gov/award/" + record["generated_internal_id"]
    )
    assert out["body"] == record["Description"]
    assert out["published_at"] == record["Start Date"]
    assert out["amount"] == record["Award Amount"]
    assert out["recipient"] == record["Recipient Name"]
    assert out["awarding_agency"] == record["Awarding Agency"]
    assert out["awarding_sub_agency"] == record["Awarding Sub Agency"]
    assert out["award_type_group"] == "grants"


def test_normalize_award_keeps_the_raw_record_untouched(page1):
    record = page1["results"][0]
    before = json.dumps(record, sort_keys=True)
    out = normalize_award(record, award_type_group="grants")
    assert out["raw"] == record
    assert json.dumps(record, sort_keys=True) == before


def test_normalize_award_titles_with_the_description(page1):
    record = page1["results"][0]
    assert (
        normalize_award(record, award_type_group="grants")["title"]
        == record["Description"]
    )


def test_normalize_award_falls_back_to_the_award_id_for_a_title():
    out = normalize_award(
        {"Award ID": "R01AI182256", "Description": ""}, award_type_group="grants"
    )
    assert out["title"] == "R01AI182256"


def test_normalize_award_uses_a_supplied_title_callable(page1):
    record = page1["results"][0]
    out = normalize_award(
        record,
        award_type_group="grants",
        title_of=lambda r: f"{r['Recipient Name']} / {r['Awarding Agency']}",
    )
    assert out["title"] == (f"{record['Recipient Name']} / {record['Awarding Agency']}")


def test_normalize_award_passes_the_amount_through_verbatim():
    assert (
        normalize_award({"Award Amount": 1341064.0}, award_type_group="grants")[
            "amount"
        ]
        == 1341064.0
    )
    assert normalize_award({}, award_type_group="grants")["amount"] is None


def test_normalize_award_tolerates_a_record_missing_every_field():
    out = normalize_award({}, award_type_group="contracts")
    assert out["external_id"] is None
    assert out["internal_id"] is None
    assert out["url"] is None
    assert out["title"] == ""
    assert out["body"] == ""
    assert out["award_type_group"] == "contracts"
    assert out["raw"] == {}


def test_normalize_award_respects_an_injected_site_url(page1):
    out = normalize_award(
        page1["results"][0], award_type_group="grants", site_url="http://example.test/"
    )
    assert out["url"].startswith("http://example.test/award/")
    assert "//award" not in out["url"].replace("http://", "")


def test_normalize_award_percent_encodes_the_internal_id_into_the_url():
    """The id is the server's string and it ends up in a link."""
    out = normalize_award(
        {"generated_internal_id": "ASST/NON 1?x=2"}, award_type_group="grants"
    )
    assert out["url"] == "https://www.usaspending.gov/award/ASST%2FNON%201%3Fx%3D2"
    assert out["internal_id"] == "ASST/NON 1?x=2"


def test_normalize_award_leaves_an_ordinary_internal_id_alone(page1):
    """Real ids are unreserved characters, so encoding must not disturb them."""
    record = page1["results"][0]
    assert normalize_award(record, award_type_group="grants")["url"].endswith(
        "/award/" + record["generated_internal_id"]
    )


def test_normalize_award_preserves_unicode():
    out = normalize_award(
        {"Description": "Évaluation des systèmes — 農業", "Recipient Name": "Ørsted"},
        award_type_group="grants",
    )
    assert out["body"] == "Évaluation des systèmes — 農業"
    assert out["recipient"] == "Ørsted"


# ------------------------------------------------------------ search_awards ---


def test_search_awards_sends_one_request_per_group(stub_http, page1, page2):
    bodies = []

    def handler(request):
        bodies.append(request.json)
        return {"results": [], "page_metadata": {"hasNext": False}}

    stub_http.route(SEARCH_PATH, handler)
    search_awards(
        start_date="2025-01-01",
        end_date="2025-03-31",
        recipient="Iowa State University",
        award_types=("grants", "contracts"),
        base_url=stub_http.url,
    )
    assert len(bodies) == 2
    assert bodies[0]["filters"]["award_type_codes"] == list(AWARD_TYPE_GROUPS["grants"])
    assert bodies[1]["filters"]["award_type_codes"] == list(
        AWARD_TYPE_GROUPS["contracts"]
    )


def test_search_awards_posts_json_to_the_documented_path(stub_http):
    stub_http.route(SEARCH_PATH, {"results": [], "page_metadata": {"hasNext": False}})
    search_awards(
        start_date="2025-01-01",
        end_date="2025-01-31",
        recipient="Iowa State University",
        award_types=("grants",),
        base_url=stub_http.url,
    )
    sent = stub_http.last()
    assert sent.method == "POST"
    assert sent.path == SEARCH_PATH
    assert sent.headers["Content-Type"] == "application/json"
    assert sent.headers["Accept"] == "application/json"
    assert sent.json["filters"]["recipient_search_text"] == ["Iowa State University"]


def test_search_awards_sends_a_unicode_recipient_as_utf8(stub_http):
    stub_http.route(SEARCH_PATH, {"results": [], "page_metadata": {"hasNext": False}})
    search_awards(
        start_date="2025-01-01",
        end_date="2025-01-31",
        recipient="Universität Zürich",
        award_types=("grants",),
        base_url=stub_http.url,
    )
    sent = stub_http.last()
    assert "Universität Zürich".encode("utf-8") in sent.body
    assert sent.json["filters"]["recipient_search_text"] == ["Universität Zürich"]
    assert sent.headers["Content-Length"] == str(len(sent.body))


def test_search_awards_tolerates_a_base_url_with_a_trailing_slash(stub_http):
    """The base_url is joined, not concatenated.

    A bare host is the wrong probe for this: http.client collapses a leading
    ``//`` back to ``/`` all on its own, so dropping the rstrip would still
    reach the route. A base_url carrying a path prefix — a proxy or a mirror —
    is where the doubled separator survives to the server.
    """
    stub_http.route(
        "/gateway" + SEARCH_PATH, {"results": [], "page_metadata": {"hasNext": False}}
    )
    search_awards(
        start_date="2025-01-01",
        end_date="2025-01-31",
        award_types=("grants",),
        base_url=stub_http.url + "/gateway/",
    )
    assert stub_http.last().path == "/gateway" + SEARCH_PATH


def test_search_awards_follows_paging_until_has_next_is_false(stub_http, page1, page2):
    pages = {1: page1, 2: page2}

    stub_http.route(SEARCH_PATH, lambda r: pages[r.json["page"]])
    out = search_awards(
        start_date="2025-01-01",
        end_date="2025-03-31",
        award_types=("grants",),
        base_url=stub_http.url,
    )
    assert [r.json["page"] for r in stub_http.requests] == [1, 2]
    assert len(out) == 3
    assert out[0]["external_id"] == page1["results"][0]["Award ID"]
    assert out[-1]["external_id"] == page2["results"][0]["Award ID"]
    assert {r["award_type_group"] for r in out} == {"grants"}


def test_search_awards_stops_at_max_pages(stub_http, page1):
    stub_http.route(SEARCH_PATH, page1)  # hasNext is always True
    out = search_awards(
        start_date="2025-01-01",
        end_date="2025-03-31",
        award_types=("grants",),
        base_url=stub_http.url,
        max_pages=3,
    )
    assert len(stub_http.requests) == 3
    assert len(out) == 6


def test_search_awards_asks_for_full_pages(stub_http):
    stub_http.route(SEARCH_PATH, {"results": [], "page_metadata": {"hasNext": False}})
    search_awards(
        start_date="2025-01-01",
        end_date="2025-01-31",
        award_types=("grants",),
        base_url=stub_http.url,
    )
    assert stub_http.last().json["limit"] == 100


def test_search_awards_returns_an_empty_list_for_no_results(stub_http):
    stub_http.route(SEARCH_PATH, load("empty"))
    assert (
        search_awards(
            start_date="2025-01-01",
            end_date="2025-01-31",
            award_types=("grants",),
            base_url=stub_http.url,
        )
        == []
    )


def test_search_awards_stops_when_page_metadata_omits_has_next(stub_http, page1):
    """A response with no hasNext key ends the group rather than looping."""
    stub_http.route(SEARCH_PATH, {"results": page1["results"], "page_metadata": {}})
    out = search_awards(
        start_date="2025-01-01",
        end_date="2025-03-31",
        award_types=("grants",),
        base_url=stub_http.url,
        max_pages=5,
    )
    assert len(out) == 2
    assert len(stub_http.requests) == 1


def test_search_awards_passes_date_type_through(stub_http):
    stub_http.route(SEARCH_PATH, {"results": [], "page_metadata": {"hasNext": False}})
    search_awards(
        start_date="2025-01-01",
        end_date="2025-01-31",
        award_types=("grants",),
        date_type="new_awards_only",
        base_url=stub_http.url,
    )
    period = stub_http.last().json["filters"]["time_period"][0]
    assert period["date_type"] == "new_awards_only"


def test_search_awards_passes_fields_and_sort_through(stub_http):
    """Without these, only contracts and grants are reachable through search."""
    stub_http.route(SEARCH_PATH, {"results": [], "page_metadata": {"hasNext": False}})
    search_awards(
        start_date="2025-01-01",
        end_date="2025-01-31",
        award_types=("loans",),
        fields=["Award ID", "Loan Value"],
        sort="Loan Value",
        base_url=stub_http.url,
    )
    body = stub_http.last().json
    assert body["fields"] == ["Award ID", "Loan Value"]
    assert body["sort"] == "Loan Value"


def test_search_awards_defaults_to_the_pinned_fields_and_sort(stub_http):
    stub_http.route(SEARCH_PATH, {"results": [], "page_metadata": {"hasNext": False}})
    search_awards(
        start_date="2025-01-01",
        end_date="2025-01-31",
        award_types=("grants",),
        base_url=stub_http.url,
    )
    body = stub_http.last().json
    assert body["fields"] == list(DEFAULT_FIELDS)
    assert body["sort"] == DEFAULT_SORT


def test_search_awards_rejects_an_unknown_group_before_any_request(stub_http):
    stub_http.route(SEARCH_PATH, {"results": [], "page_metadata": {"hasNext": False}})
    with pytest.raises(ValueError, match="grnats"):
        search_awards(
            start_date="2025-01-01",
            end_date="2025-01-31",
            award_types=("grnats",),
            base_url=stub_http.url,
        )
    assert stub_http.requests == []


def test_search_awards_rejects_an_empty_award_types(stub_http):
    with pytest.raises(ValueError, match="award_types"):
        search_awards(
            start_date="2025-01-01",
            end_date="2025-01-31",
            award_types=(),
            base_url=stub_http.url,
        )


# ------------------------------------------------------------- error paths ---


def test_search_awards_raises_on_a_non_2xx(stub_http):
    body = json.dumps(load("mixed_group_422")).encode()
    stub_http.route(SEARCH_PATH, Response(body=body, status=422))
    with pytest.raises(UsaspendingError) as exc:
        search_awards(
            start_date="2025-01-01",
            end_date="2025-01-31",
            award_types=("grants",),
            base_url=stub_http.url,
        )
    assert "422" in str(exc.value)
    assert "must only contain types from one group" in str(exc.value)


def test_search_awards_raises_on_a_500(stub_http):
    stub_http.down = True
    with pytest.raises(UsaspendingError) as exc:
        search_awards(
            start_date="2025-01-01",
            end_date="2025-01-31",
            award_types=("grants",),
            base_url=stub_http.url,
        )
    assert "503" in str(exc.value)


def test_search_awards_raises_when_the_host_is_unreachable(unreachable_url):
    with pytest.raises(UsaspendingError):
        search_awards(
            start_date="2025-01-01",
            end_date="2025-01-31",
            award_types=("grants",),
            base_url=unreachable_url,
            timeout_s=2.0,
        )


def test_search_awards_raises_on_a_timeout(stub_http):
    def slow(request):
        time.sleep(0.4)
        return {"results": [], "page_metadata": {"hasNext": False}}

    stub_http.route(SEARCH_PATH, slow)
    with pytest.raises(UsaspendingError):
        search_awards(
            start_date="2025-01-01",
            end_date="2025-01-31",
            award_types=("grants",),
            base_url=stub_http.url,
            timeout_s=0.05,
        )


def test_search_awards_raises_on_a_malformed_body(stub_http):
    stub_http.route(SEARCH_PATH, Response(body=b"<html>gateway</html>", status=200))
    with pytest.raises(UsaspendingError, match="JSON"):
        search_awards(
            start_date="2025-01-01",
            end_date="2025-01-31",
            award_types=("grants",),
            base_url=stub_http.url,
        )


def test_search_awards_raises_when_results_is_missing(stub_http):
    stub_http.route(SEARCH_PATH, {"page_metadata": {"hasNext": False}})
    with pytest.raises(UsaspendingError, match="results"):
        search_awards(
            start_date="2025-01-01",
            end_date="2025-01-31",
            award_types=("grants",),
            base_url=stub_http.url,
        )


def test_search_awards_raises_when_results_holds_a_non_record(stub_http):
    """A junk element is a UsaspendingError, never an AttributeError."""
    stub_http.route(
        SEARCH_PATH, {"results": ["oops"], "page_metadata": {"hasNext": False}}
    )
    with pytest.raises(UsaspendingError, match="award record"):
        search_awards(
            start_date="2025-01-01",
            end_date="2025-01-31",
            award_types=("grants",),
            base_url=stub_http.url,
        )


def test_search_awards_raises_when_results_holds_a_null(stub_http):
    """null is the element a sentinel-based guard silently lets through."""
    stub_http.route(
        SEARCH_PATH, {"results": [None], "page_metadata": {"hasNext": False}}
    )
    with pytest.raises(UsaspendingError, match="award record"):
        search_awards(
            start_date="2025-01-01",
            end_date="2025-01-31",
            award_types=("grants",),
            base_url=stub_http.url,
        )


def test_search_awards_stops_when_page_metadata_is_not_an_object(stub_http, page1):
    """Junk paging metadata ends the group; it must not crash the call."""
    stub_http.route(SEARCH_PATH, {"results": page1["results"], "page_metadata": "nope"})
    out = search_awards(
        start_date="2025-01-01",
        end_date="2025-03-31",
        award_types=("grants",),
        base_url=stub_http.url,
        max_pages=5,
    )
    assert len(out) == 2
    assert len(stub_http.requests) == 1


def test_search_awards_never_returns_a_partial_result(stub_http, page1):
    """Page 1 succeeded, page 2 failed: the whole call raises."""

    def handler(request):
        if request.json["page"] == 1:
            return page1
        return Response(body={"detail": "boom"}, status=500)

    stub_http.route(SEARCH_PATH, handler)
    with pytest.raises(UsaspendingError):
        search_awards(
            start_date="2025-01-01",
            end_date="2025-03-31",
            award_types=("grants",),
            base_url=stub_http.url,
        )


def test_search_awards_fails_the_whole_call_when_the_second_group_fails(
    stub_http, page2
):
    def handler(request):
        if request.json["filters"]["award_type_codes"][0] == "02":
            return page2
        return Response(body={"detail": "boom"}, status=500)

    stub_http.route(SEARCH_PATH, handler)
    with pytest.raises(UsaspendingError):
        search_awards(
            start_date="2025-01-01",
            end_date="2025-03-31",
            award_types=("grants", "contracts"),
            base_url=stub_http.url,
        )


def test_search_awards_does_not_import_anything_from_brief():
    """A seed graduates only if it stands alone."""
    source = (
        Path(__file__).resolve().parents[1]
        / "src/brief/lib/usaspending_award_search.py"
    ).read_text(encoding="utf-8")
    assert "from brief" not in source
    assert "import brief" not in source
