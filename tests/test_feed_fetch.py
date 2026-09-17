"""Tests for the feed-fetch seed.

Real sockets only: every HTTP path goes through the vendored ``stub_http``
server on an ephemeral port. No test touches the live network.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from brief.lib.feed_fetch import (
    DEFAULT_USER_AGENT,
    FeedFetchError,
    fetch_feed,
    parse_feed_bytes,
    strip_html,
)
from harness.stub_http import Response, stub_http, unreachable_url  # noqa: F401

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RSS_BYTES = (FIXTURES / "feed_fetch_rss.xml").read_bytes()
ATOM_BYTES = (FIXTURES / "feed_fetch_atom.xml").read_bytes()


def sent(request, name: str) -> str | None:
    """Header value as the server received it, matched case-insensitively."""
    for key, value in request.headers.items():
        if key.lower() == name.lower():
            return value
    return None


# ----------------------------------------------------------- what we send ---


def test_sends_get_with_browser_user_agent_and_preserves_query(stub_http):
    # The stub routes on the raw request line, so this also proves the query
    # string reached the server untouched.
    stub_http.route("/feed.xml?tag=ai&limit=5", RSS_BYTES)

    fetch_feed(f"{stub_http.url}/feed.xml?tag=ai&limit=5")

    request = stub_http.requests[-1]
    assert request.method == "GET"
    assert request.path == "/feed.xml?tag=ai&limit=5"
    assert sent(request, "User-Agent") == DEFAULT_USER_AGENT
    assert "Mozilla/5.0" in DEFAULT_USER_AGENT
    assert "python" not in sent(request, "User-Agent").lower()


def test_caller_can_override_the_user_agent(stub_http):
    stub_http.route("/feed.xml", RSS_BYTES)

    fetch_feed(f"{stub_http.url}/feed.xml", user_agent="brief/1.0 (+https://x.test)")

    assert sent(stub_http.requests[-1], "User-Agent") == "brief/1.0 (+https://x.test)"


def test_sends_conditional_headers_when_validators_are_supplied(stub_http):
    stub_http.route("/feed.xml", RSS_BYTES)

    fetch_feed(
        f"{stub_http.url}/feed.xml",
        etag='W/"abc123"',
        last_modified="Tue, 16 Sep 2026 14:30:00 GMT",
    )

    request = stub_http.requests[-1]
    assert sent(request, "If-None-Match") == 'W/"abc123"'
    assert sent(request, "If-Modified-Since") == "Tue, 16 Sep 2026 14:30:00 GMT"


def test_first_fetch_sends_no_conditional_headers(stub_http):
    stub_http.route("/feed.xml", RSS_BYTES)

    fetch_feed(f"{stub_http.url}/feed.xml")

    request = stub_http.requests[-1]
    assert sent(request, "If-None-Match") is None
    assert sent(request, "If-Modified-Since") is None


# -------------------------------------------------------- what we give back ---


def test_200_returns_response_validators_and_parsed_entries(stub_http):
    stub_http.route(
        "/feed.xml",
        Response(
            RSS_BYTES,
            headers={
                "ETag": 'W/"new-etag"',
                "Last-Modified": "Wed, 17 Sep 2026 06:00:00 GMT",
            },
        ),
    )

    result = fetch_feed(f"{stub_http.url}/feed.xml", etag='W/"old-etag"')

    assert result["status"] == 200
    assert result["etag"] == 'W/"new-etag"'
    assert result["last_modified"] == "Wed, 17 Sep 2026 06:00:00 GMT"
    assert [e["id"] for e in result["entries"]] == ["urn:campus:1", "urn:campus:2"]


def test_200_without_validator_headers_reports_none_not_the_stale_input(stub_http):
    stub_http.route("/feed.xml", RSS_BYTES)

    result = fetch_feed(
        f"{stub_http.url}/feed.xml",
        etag='W/"old-etag"',
        last_modified="Tue, 16 Sep 2026 14:30:00 GMT",
    )

    assert result["status"] == 200
    assert result["etag"] is None
    assert result["last_modified"] is None


def test_304_echoes_the_input_validators_and_returns_no_entries(stub_http):
    stub_http.route("/feed.xml", Response(b"", status=304))

    result = fetch_feed(
        f"{stub_http.url}/feed.xml",
        etag='W/"abc123"',
        last_modified="Tue, 16 Sep 2026 14:30:00 GMT",
    )

    assert result == {
        "status": 304,
        "etag": 'W/"abc123"',
        "last_modified": "Tue, 16 Sep 2026 14:30:00 GMT",
        "entries": [],
    }


# ------------------------------------------------------------- error paths ---


def test_non_200_status_raises_naming_the_url_and_the_status(stub_http):
    url = f"{stub_http.url}/feed.xml"
    stub_http.route("/feed.xml", Response(b"nope", status=500))

    with pytest.raises(FeedFetchError) as excinfo:
        fetch_feed(url)

    message = str(excinfo.value)
    assert url in message
    assert "500" in message


def test_404_raises_rather_than_returning_an_empty_result(stub_http):
    with pytest.raises(FeedFetchError) as excinfo:
        fetch_feed(f"{stub_http.url}/missing.xml")

    assert "404" in str(excinfo.value)


def test_connection_failure_raises_naming_the_url(unreachable_url):
    with pytest.raises(FeedFetchError) as excinfo:
        fetch_feed(f"{unreachable_url}/feed.xml", timeout_s=2.0)

    assert unreachable_url in str(excinfo.value)


def test_timeout_raises(stub_http):
    def slow(request):
        time.sleep(0.6)
        return RSS_BYTES

    stub_http.route("/slow.xml", slow)

    with pytest.raises(FeedFetchError):
        fetch_feed(f"{stub_http.url}/slow.xml", timeout_s=0.1)


def test_malformed_body_raises(stub_http):
    stub_http.route("/feed.xml", b"<<< this is not a feed at all >>>")

    with pytest.raises(FeedFetchError) as excinfo:
        fetch_feed(f"{stub_http.url}/feed.xml")

    assert "entries" in str(excinfo.value).lower()


def test_well_formed_feed_with_no_items_raises(stub_http):
    empty = (
        b'<?xml version="1.0"?><rss version="2.0"><channel>'
        b"<title>Nothing</title></channel></rss>"
    )
    stub_http.route("/feed.xml", empty)

    with pytest.raises(FeedFetchError):
        fetch_feed(f"{stub_http.url}/feed.xml")


# ------------------------------------------------------- entry normalization ---


def test_entry_fields_are_plain_text_and_unicode_survives():
    entry = parse_feed_bytes(RSS_BYTES)[0]

    assert entry == {
        "id": "urn:campus:1",
        "url": "https://example.edu/news/1",
        "title": "Résumé of the year & beyond",
        "summary": "Grant awarded to the lab. Café talk follows.",
        "published_at": "2026-09-16T14:30:00Z",
    }


def test_url_falls_back_to_the_id_when_the_entry_has_no_link():
    entry = parse_feed_bytes(RSS_BYTES)[1]

    assert entry["id"] == "urn:campus:2"
    assert entry["url"] == "urn:campus:2"


def test_entry_with_neither_link_nor_id_is_dropped():
    entries = parse_feed_bytes(RSS_BYTES)

    assert len(entries) == 2
    assert all("Third item" not in e["title"] for e in entries)


def test_published_at_falls_back_to_updated_for_atom():
    entry = parse_feed_bytes(ATOM_BYTES)[0]

    assert entry["published_at"] == "2026-09-15T08:00:00Z"


def test_relative_entry_link_is_resolved_against_base_url():
    entry = parse_feed_bytes(ATOM_BYTES, base_url="https://example.edu/feeds/atom.xml")[
        0
    ]

    assert entry["url"] == "https://example.edu/articles/a1"


def test_fetch_feed_resolves_relative_links_against_the_feed_url(stub_http):
    stub_http.route("/feeds/atom.xml", ATOM_BYTES)

    result = fetch_feed(f"{stub_http.url}/feeds/atom.xml")

    assert result["entries"][0]["url"] == f"{stub_http.url}/articles/a1"


def test_entry_without_a_date_reports_published_at_none():
    body = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><item>'
        b"<title>Undated</title><link>https://example.edu/u</link>"
        b"<description>No date here.</description></item></channel></rss>"
    )

    assert parse_feed_bytes(body)[0]["published_at"] is None


def test_parse_feed_bytes_returns_empty_list_for_garbage():
    assert parse_feed_bytes(b"not a feed") == []


# ---------------------------------------------------------------- strip_html ---


def test_strip_html_removes_tags_decodes_entities_and_collapses_whitespace():
    assert strip_html("<p>A &amp; B</p>\n  <p>C   D</p>") == "A & B C D"


def test_strip_html_leaves_plain_text_alone():
    assert strip_html("Café talk") == "Café talk"


def test_strip_html_on_empty_input_returns_empty_string():
    assert strip_html("") == ""


def test_block_tags_are_a_word_break_even_with_no_whitespace_around_them():
    assert strip_html("<p>one</p><p>two</p>") == "one two"


def test_text_after_a_closing_block_tag_is_a_separate_word():
    assert strip_html("<p>one</p>two") == "one two"


def test_self_closing_break_separates_words():
    assert strip_html("a<br/>b") == "a b"


def test_inline_tags_do_not_insert_spaces():
    assert strip_html("con<b>cat</b>enate") == "concatenate"


def test_strip_html_survives_unclosed_tags():
    assert strip_html("<div><b>bold <i>x") == "bold x"


# --------------------------------------------------------------- open_url seam ---


def test_open_url_seam_receives_plain_values_and_replaces_the_network():
    seen: list[tuple] = []

    def transport(url, headers, timeout_s):
        seen.append((url, dict(headers), timeout_s))
        return 200, {"ETag": 'W/"seam"'}, RSS_BYTES

    result = fetch_feed(
        "https://feeds.invalid/feed.xml",
        etag='W/"prev"',
        timeout_s=7.5,
        open_url=transport,
    )

    url, headers, timeout_s = seen[0]
    assert url == "https://feeds.invalid/feed.xml"
    assert headers["User-Agent"] == DEFAULT_USER_AGENT
    assert headers["If-None-Match"] == 'W/"prev"'
    assert timeout_s == 7.5
    assert result["etag"] == 'W/"seam"'
    assert len(result["entries"]) == 2


def test_response_headers_are_read_case_insensitively():
    def transport(url, headers, timeout_s):
        return (
            200,
            {"etag": 'W/"lower"', "last-modified": "Wed, 17 Sep 2026 06:00:00 GMT"},
            RSS_BYTES,
        )

    result = fetch_feed("https://feeds.invalid/feed.xml", open_url=transport)

    assert result["etag"] == 'W/"lower"'
    assert result["last_modified"] == "Wed, 17 Sep 2026 06:00:00 GMT"


def test_transport_failure_is_wrapped_in_feed_fetch_error():
    def transport(url, headers, timeout_s):
        raise OSError("socket exploded")

    with pytest.raises(FeedFetchError) as excinfo:
        fetch_feed("https://feeds.invalid/feed.xml", open_url=transport)

    assert "socket exploded" in str(excinfo.value)


def test_transport_seam_is_not_scheme_restricted():
    # The http/https guard belongs to the default urllib transport, not to
    # fetch_feed: a caller's own transport keeps the URL space it wants.
    def transport(url, headers, timeout_s):
        return 200, {}, RSS_BYTES

    result = fetch_feed("playwright://internal/feed", open_url=transport)

    assert len(result["entries"]) == 2


# ------------------------------------------------- scheme and input guards ---


def test_default_transport_refuses_file_urls_and_does_not_read_the_file(tmp_path):
    secret = tmp_path / "local_feed.xml"
    secret.write_bytes(RSS_BYTES)

    with pytest.raises(FeedFetchError) as excinfo:
        fetch_feed(secret.as_uri())

    message = str(excinfo.value)
    assert "file" in message
    assert str(secret) in message or secret.as_uri() in message


def test_default_transport_refuses_a_url_with_no_scheme_rather_than_raising_valueerror():
    # urllib.request.Request raises a bare ValueError here; the contract says
    # every fetch failure arrives as FeedFetchError naming the URL.
    with pytest.raises(FeedFetchError) as excinfo:
        fetch_feed("feeds.invalid/feed.xml")

    assert "feeds.invalid/feed.xml" in str(excinfo.value)


def test_parse_feed_bytes_refuses_a_string_instead_of_opening_it(tmp_path):
    # feedparser.parse() opens a string as a resource: a str here would make a
    # "pure" function read disk (shown below) or hit the network with
    # feedparser's own headers.
    local = tmp_path / "local_feed.xml"
    local.write_bytes(RSS_BYTES)

    with pytest.raises(TypeError) as excinfo:
        parse_feed_bytes(local.as_uri())

    assert "bytes" in str(excinfo.value)


def test_parse_feed_bytes_accepts_a_bytearray():
    assert len(parse_feed_bytes(bytearray(RSS_BYTES))) == 2


def test_strip_html_drops_script_and_style_source():
    html = "<p>Real text</p><script>alert(1)</script><style>p{color:red}</style>"

    assert strip_html(html) == "Real text"


def test_strip_html_keeps_text_after_a_script_block_separate():
    assert strip_html("a<script>var x=1</script>b") == "a b"
