"""Tests for the crates-io-search seed.

Real sockets through tests/harness/stub_http.py and a real trimmed response
in tests/fixtures/crates_io_search.json, including a crate with no
description and one with no repository — both occur live.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from brief.lib import crates_io_search as ci
from harness.stub_http import Response
from harness.stub_http import stub_http as stub_http_server  # noqa: F401

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CRATES = json.loads((FIXTURES / "crates_io_search.json").read_text(encoding="utf-8"))
UA = "topic-brief (mailto:t@example.test)"


class _PathOnlyRoutes(dict):
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


def hits(stub, path="/crates"):
    return [r for r in stub.requests if urlsplit(r.path).path == path]


def query_of(request) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(request.path).query).items()}


def search(stub, **kw):
    kw.setdefault("query", "bioinformatics")
    kw.setdefault("user_agent", UA)
    kw["base_url"] = stub.url
    return ci.search_crates(**kw)


# ------------------------------------------------------------------- sort ---


def test_an_unknown_sort_is_refused_before_any_request(stub):
    """crates.io ACCEPTS an unknown sort and falls back to relevance, which
    returns old popular crates. That reads as a quiet week rather than as a
    typo, so it must cost an exception."""
    stub.route("/crates", b"{}")

    with pytest.raises(ValueError) as caught:
        search(stub, sort="newest")

    assert "relevance" in str(caught.value), "the message says why it matters"
    assert not hits(stub), "and no request was made"


@pytest.mark.parametrize("sort", sorted(ci.SORTS))
def test_every_declared_sort_is_accepted_and_passed_through(stub, sort):
    stub.route("/crates", json.dumps({"meta": {}, "crates": []}).encode())

    search(stub, sort=sort)

    assert query_of(hits(stub)[-1])["sort"] == sort


def test_published_at_follows_the_sort(stub):
    """'What is new' and 'what moved' want different dates from one record.
    Returning one silently makes the other wrong."""
    raw = {"name": "x", "created_at": "2026-01-01T00:00:00Z",
           "updated_at": "2026-09-01T00:00:00Z"}

    assert ci.normalize_crate(raw, sort="new")["published_at"].startswith("2026-01-01")
    assert ci.normalize_crate(raw, sort="recent-update")["published_at"].startswith("2026-09-01")
    assert ci.normalize_crate(raw, sort="new")["created_at"].startswith("2026-01-01")
    assert ci.normalize_crate(raw, sort="new")["updated_at"].startswith("2026-09-01")


# ---------------------------------------------------------------- shaping ---


def test_a_real_response_shapes_into_flat_records(stub):
    stub.route("/crates", json.dumps(CRATES).encode())

    crates = search(stub)

    assert len(crates) == len(CRATES["crates"])
    for c in crates:
        assert set(c) == {
            "name", "description", "url", "repository", "homepage",
            "documentation", "version", "downloads", "recent_downloads",
            "keywords", "categories", "created_at", "updated_at",
            "published_at", "raw",
        }


def test_a_crate_with_no_description_or_repository_is_empty_not_missing(stub):
    """The fixture carries both, taken from live data. A None reaching a
    caller that expects a string is the failure this shape prevents."""
    stub.route("/crates", json.dumps(CRATES).encode())

    crates = search(stub)

    assert any(c["description"] == "" for c in crates)
    assert any(c["repository"] == "" for c in crates)
    assert all(isinstance(c["description"], str) for c in crates)
    assert all(isinstance(c["keywords"], list) for c in crates)


def test_every_crate_gets_a_url_on_the_registry(stub):
    stub.route("/crates", json.dumps(CRATES).encode())

    for c in search(stub):
        assert c["url"].startswith("https://crates.io/crates/")


def test_a_name_needing_escaping_is_quoted():
    assert ci.normalize_crate({"name": "a b"})["url"].endswith("/a%20b")


# ----------------------------------------------------------------- paging ---


def test_paging_follows_the_registrys_own_next_page(stub):
    """The registry hands back a ready-made query string and deprecated
    offset paging; building our own offsets would silently truncate."""
    pages = [
        {"meta": {"next_page": "?q=x&seek=abc"}, "crates": [{"name": "one"}]},
        {"meta": {"next_page": None}, "crates": [{"name": "two"}]},
    ]
    calls = []

    def handler(request):
        calls.append(request)
        return json.dumps(pages[len(calls) - 1]).encode()

    stub.route("/crates", handler)

    crates = search(stub)

    assert [c["name"] for c in crates] == ["one", "two"]
    assert query_of(calls[1]).get("seek") == "abc", "the registry's cursor is used"


def test_max_pages_bounds_the_walk(stub):
    def handler(request):
        return json.dumps({"meta": {"next_page": "?seek=more"},
                           "crates": [{"name": f"c{len(hits(stub))}"}]}).encode()

    stub.route("/crates", handler)

    crates = search(stub, max_pages=3)

    assert len(hits(stub)) == 3 and len(crates) == 3


def test_a_crate_repeated_across_pages_is_returned_once(stub):
    pages = [
        {"meta": {"next_page": "?seek=b"}, "crates": [{"name": "dup"}]},
        {"meta": {"next_page": None}, "crates": [{"name": "dup"}, {"name": "new"}]},
    ]
    calls = []

    def handler(request):
        calls.append(1)
        return json.dumps(pages[len(calls) - 1]).encode()

    stub.route("/crates", handler)

    assert [c["name"] for c in search(stub)] == ["dup", "new"]


@pytest.mark.parametrize("bad", [0, -1, 101])
def test_an_out_of_range_per_page_is_refused_before_any_request(stub, bad):
    stub.route("/crates", b"{}")
    with pytest.raises(ValueError):
        search(stub, per_page=bad)
    assert not hits(stub)


# ------------------------------------------------------------------ policy ---


def test_a_user_agent_without_a_contact_is_refused(stub):
    """crates.io's crawler policy asks callers to identify themselves. An
    anonymous crawler is how a project gets blocked for everyone."""
    stub.route("/crates", b"{}")

    with pytest.raises(ValueError):
        search(stub, user_agent="topic-brief")

    assert not hits(stub)


def test_the_user_agent_reaches_the_server(stub):
    stub.route("/crates", json.dumps({"meta": {}, "crates": []}).encode())

    search(stub)

    assert hits(stub)[-1].headers.get("User-Agent") == UA


# ------------------------------------------------------------------ errors ---


def test_a_non_200_names_the_status(stub):
    stub.route("/crates", Response(b"nope", status=500))
    with pytest.raises(ci.CratesIoError) as caught:
        search(stub)
    assert "500" in str(caught.value)


def test_an_unparseable_body_is_an_error_not_an_empty_result(stub):
    stub.route("/crates", b"<html>")
    with pytest.raises(ci.CratesIoError):
        search(stub)


def test_a_gzip_body_is_decompressed(stub):
    stub.route("/crates", Response(gzip.compress(json.dumps(CRATES).encode()),
                                   headers={"Content-Encoding": "gzip"}))
    assert len(search(stub)) == len(CRATES["crates"])


def test_a_bot_wall_is_named_rather_than_parsed(stub):
    stub.route("/crates", Response(
        b"<html><head><title>Just a moment...</title></head></html>",
        status=403, headers={"Server": "cloudflare"}))
    with pytest.raises(ci.CratesIoBlockedError) as caught:
        search(stub)
    assert "bot wall" in str(caught.value)


def test_an_unreachable_server_raises_the_modules_own_error():
    with pytest.raises(ci.CratesIoError):
        ci.search_crates("x", user_agent=UA, base_url="http://127.0.0.1:1")


# -------------------------------------------------------------- boundaries ---


def test_the_module_imports_only_the_standard_library():
    import ast

    tree = ast.parse(Path(ci.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and not node.level:
                roots.add(node.module.split(".")[0])
    assert roots == {"__future__", "json", "re", "urllib", "typing", "gzip", "zlib"}


def test_the_seed_does_not_import_from_the_app():
    source = Path(ci.__file__).read_text(encoding="utf-8")
    assert "from brief" not in source and "import brief" not in source
