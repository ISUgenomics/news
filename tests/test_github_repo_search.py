"""Tests for the github-repo-search seed.

Real sockets, and a real trimmed response in
tests/fixtures/github_repo_search.json carrying a repo with no description,
one with no licence, one fork and one archived.

The two tests that matter most are the ones about lying successes: a 200
carrying `incomplete_results`, and a spent rate limit reported as 403.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from brief.lib import github_repo_search as gh
from harness.stub_http import Response
from harness.stub_http import stub_http as stub_http_server  # noqa: F401

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPOS = json.loads((FIXTURES / "github_repo_search.json").read_text(encoding="utf-8"))
UA = "topic-brief (mailto:t@example.test)"
PATH = "/search/repositories"


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


def hits(stub):
    return [r for r in stub.requests if urlsplit(r.path).path == PATH]


def query_of(request) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(request.path).query).items()}


def search(stub, **kw):
    kw.setdefault("query", "language:rust topic:bioinformatics")
    kw.setdefault("user_agent", UA)
    kw["base_url"] = stub.url
    return gh.search_repositories(**kw)


def ok(payload) -> bytes:
    return json.dumps(payload).encode()


# ------------------------------------------------- the two lying successes ---


def test_incomplete_results_raises_rather_than_reporting_a_quiet_week(stub):
    """GitHub answers 200 with a partial set and says so in a FIELD, not the
    status. Read as success, a truncated search becomes 'nothing happened'."""
    stub.route(PATH, ok({"total_count": 900, "incomplete_results": True,
                         "items": REPOS["items"][:2]}))

    with pytest.raises(gh.GitHubIncompleteResultsError) as caught:
        search(stub)

    assert "incomplete_results" in str(caught.value)
    assert len(caught.value.results) == 2, "what did arrive is carried on the error"


def test_a_spent_rate_limit_is_its_own_error_with_the_reset_time(stub):
    """GitHub reports a spent SEARCH limit as 403, which is otherwise
    indistinguishable from a permissions problem. The header is the tell."""
    stub.route(PATH, Response(b'{"message":"rate limit"}', status=403,
                              headers={"x-ratelimit-remaining": "0",
                                       "x-ratelimit-reset": "1789673736"}))

    with pytest.raises(gh.GitHubRateLimitError) as caught:
        search(stub)

    assert caught.value.reset_at.startswith("2026-"), "the reset is decoded"
    assert "10 requests/minute" in str(caught.value)


def test_a_real_403_is_not_reported_as_a_rate_limit(stub):
    """Without the header it is a permissions failure, and saying 'rate
    limit' would send the operator to wait for a window that never opens."""
    stub.route(PATH, Response(b'{"message":"forbidden"}', status=403,
                              headers={"x-ratelimit-remaining": "17"}))

    with pytest.raises(gh.GitHubSearchError) as caught:
        search(stub)

    assert not isinstance(caught.value, gh.GitHubRateLimitError)
    assert "403" in str(caught.value)


def test_nothing_sleeps_waiting_for_the_reset(stub):
    """A seed that sleeps turns a fast failure into a hung job."""
    import time

    stub.route(PATH, Response(b"{}", status=403,
                              headers={"x-ratelimit-remaining": "0",
                                       "x-ratelimit-reset": "9999999999"}))

    started = time.monotonic()
    with pytest.raises(gh.GitHubRateLimitError):
        search(stub)

    assert time.monotonic() - started < 2.0
    assert len(hits(stub)) == 1, "and it did not retry"


# ----------------------------------------------------------------- request ---


def test_the_query_is_passed_through_verbatim(stub):
    """GitHub's qualifier grammar is the API's. Rebuilding it here would be a
    second grammar to get subtly wrong."""
    stub.route(PATH, ok({"items": [], "incomplete_results": False}))
    q = "language:rust topic:bioinformatics created:>2026-08-18 NOT fork:true"

    search(stub, query=q)

    assert query_of(hits(stub)[-1])["q"] == q


def test_a_token_is_sent_as_a_bearer_when_given_and_omitted_when_not(stub):
    stub.route(PATH, ok({"items": [], "incomplete_results": False}))

    search(stub)
    assert "Authorization" not in hits(stub)[-1].headers

    search(stub, token="ghp_example")
    assert hits(stub)[-1].headers.get("Authorization") == "Bearer ghp_example"


def test_a_missing_user_agent_is_refused_before_any_request(stub):
    stub.route(PATH, b"{}")
    with pytest.raises(ValueError):
        search(stub, user_agent="")
    assert not hits(stub)


@pytest.mark.parametrize("bad", ["newest", "date", ""])
def test_an_unknown_sort_is_refused(stub, bad):
    stub.route(PATH, b"{}")
    with pytest.raises(ValueError):
        search(stub, sort=bad)
    assert not hits(stub)


def test_no_sort_means_best_match_and_sends_no_sort_parameter(stub):
    stub.route(PATH, ok({"items": [], "incomplete_results": False}))
    search(stub)
    assert "sort" not in query_of(hits(stub)[-1])


def test_an_empty_query_is_refused(stub):
    stub.route(PATH, b"{}")
    with pytest.raises(ValueError):
        search(stub, query="   ")
    assert not hits(stub)


# ----------------------------------------------------------------- shaping ---


def test_a_real_response_shapes_into_flat_records(stub):
    stub.route(PATH, ok(REPOS))

    repos = search(stub)

    assert len(repos) == len(REPOS["items"])
    for r in repos:
        assert set(r) == {
            "full_name", "owner", "url", "description", "homepage", "language",
            "topics", "stars", "forks", "open_issues", "license", "archived",
            "fork", "created_at", "pushed_at", "updated_at", "published_at", "raw",
        }


def test_a_repo_with_no_description_or_licence_is_empty_not_none(stub):
    stub.route(PATH, ok(REPOS))

    repos = search(stub)

    assert any(r["description"] == "" for r in repos)
    assert any(r["license"] == "" for r in repos)
    assert all(isinstance(r["description"], str) for r in repos)


def test_forks_and_archived_repos_are_returned_not_filtered(stub):
    """A digest usually wants neither; a survey of activity wants both. The
    seed reports the flags and the caller judges."""
    stub.route(PATH, ok(REPOS))

    repos = search(stub)

    assert any(r["fork"] for r in repos), "the fixture carries one"
    assert any(r["archived"] for r in repos), "and one archived"
    assert len(repos) == len(REPOS["items"]), "neither was dropped"


def test_published_at_is_the_creation_date(stub):
    stub.route(PATH, ok(REPOS))
    for r in search(stub):
        assert r["published_at"] == r["created_at"]


# ------------------------------------------------------------------ paging ---


def test_paging_stops_on_a_short_page(stub):
    stub.route(PATH, ok({"incomplete_results": False, "items": REPOS["items"][:2]}))

    search(stub, per_page=100)

    assert len(hits(stub)) == 1, "a short page means there is no more"


def test_max_pages_bounds_the_walk(stub):
    def handler(request):
        n = len(hits(stub))
        return ok({"incomplete_results": False,
                   "items": [{"full_name": f"o/r{n}-{i}"} for i in range(2)]})

    stub.route(PATH, handler)

    repos = search(stub, per_page=2, max_pages=3)

    assert len(hits(stub)) == 3 and len(repos) == 6
    assert [query_of(h)["page"] for h in hits(stub)] == ["1", "2", "3"]


def test_a_repo_repeated_across_pages_is_returned_once(stub):
    def handler(request):
        return ok({"incomplete_results": False,
                   "items": [{"full_name": "o/same"}, {"full_name": f"o/p{len(hits(stub))}"}]})

    stub.route(PATH, handler)

    repos = search(stub, per_page=2, max_pages=2)

    assert [r["full_name"] for r in repos].count("o/same") == 1


# ------------------------------------------------------------------ errors ---


def test_a_non_200_names_the_status(stub):
    stub.route(PATH, Response(b"nope", status=500))
    with pytest.raises(gh.GitHubSearchError) as caught:
        search(stub)
    assert "500" in str(caught.value)


def test_an_unparseable_body_is_an_error(stub):
    stub.route(PATH, b"<html>")
    with pytest.raises(gh.GitHubSearchError):
        search(stub)


def test_a_gzip_body_is_decompressed(stub):
    stub.route(PATH, Response(gzip.compress(ok(REPOS)),
                              headers={"Content-Encoding": "gzip"}))
    assert len(search(stub)) == len(REPOS["items"])


def test_a_bot_wall_is_named(stub):
    stub.route(PATH, Response(b"<html><head><title>Just a moment...</title></head></html>",
                              status=403, headers={"Server": "cloudflare"}))
    with pytest.raises(gh.GitHubBlockedError):
        search(stub)


def test_an_unreachable_server_raises_the_modules_own_error():
    with pytest.raises(gh.GitHubSearchError):
        gh.search_repositories("x", user_agent=UA, base_url="http://127.0.0.1:1")


# -------------------------------------------------------------- boundaries ---


def test_the_module_imports_only_the_standard_library():
    import ast

    tree = ast.parse(Path(gh.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and not node.level:
                roots.add(node.module.split(".")[0])
    assert roots == {"__future__", "json", "re", "urllib", "typing", "gzip", "zlib", "datetime"}


def test_the_seed_does_not_import_from_the_app():
    source = Path(gh.__file__).read_text(encoding="utf-8")
    assert "from brief" not in source and "import brief" not in source


@pytest.mark.parametrize(
    "response",
    [
        Response(b"nope", status=500),
        Response(b"<html>", status=200),
        Response(b"{}", status=403, headers={"x-ratelimit-remaining": "0"}),
    ],
    ids=["server-error", "unparseable", "rate-limited"],
)
def test_no_error_message_ever_contains_the_token(stub, response):
    """Checked on every failure path, not by grepping the source: the token
    legitimately appears in the Authorization header, so the property that
    matters is behavioural — it must not escape into anything a caller logs
    or a reader sees."""
    secret = "ghp_thisMustNeverBeQuoted"
    stub.route(PATH, response)

    with pytest.raises(gh.GitHubSearchError) as caught:
        search(stub, token=secret)

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
