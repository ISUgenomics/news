"""Tests for the page-main-text seed.

Stubs, never mocks: a real HTTP server on an ephemeral port serves the
fixtures, and the pure extraction path runs against saved bytes. No test
touches the live network.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from brief.lib.page_main_text import (
    DEFAULT_USER_AGENT,
    PageFetchError,
    _title_from,
    extract_main_text,
    page_main_text,
)
from harness.stub_http import Response, stub_http, unreachable_url  # noqa: F401

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ARTICLE = (FIXTURES / "page_main_text_article.html").read_bytes()
BOILERPLATE = (FIXTURES / "page_main_text_boilerplate_only.html").read_bytes()
LATIN1 = (FIXTURES / "page_main_text_latin1.html").read_bytes()
WHITESPACE = (FIXTURES / "page_main_text_whitespace.html").read_bytes()

FIRST_SENTENCE = "The first paragraph explains the finding"


def html_response(body: bytes, status: int = 200) -> Response:
    return Response(
        body=body, status=status, headers={"Content-Type": "text/html; charset=utf-8"}
    )


# ------------------------------------------------------------- fetching ---


def test_fetches_the_page_and_returns_its_main_text(stub_http):
    stub_http.route("/article.html", html_response(ARTICLE))

    result = page_main_text(f"{stub_http.url}/article.html")

    assert result["status"] == 200
    assert result["truncated"] is False
    assert result["title"] == "Headline of the Article"
    assert FIRST_SENTENCE in result["text"]
    assert "qualifications" in result["text"]


def test_sends_a_browser_like_user_agent_by_default(stub_http):
    stub_http.route("/article.html", html_response(ARTICLE))

    page_main_text(f"{stub_http.url}/article.html")

    sent = stub_http.last("/article.html")
    assert sent.method == "GET"
    assert sent.body == b""
    assert sent.headers["User-Agent"] == DEFAULT_USER_AGENT
    assert "Mozilla/5.0" in DEFAULT_USER_AGENT
    assert "Python" not in sent.headers["User-Agent"]
    assert "text/html" in sent.headers["Accept"]


def test_user_agent_override_is_the_header_that_is_sent(stub_http):
    stub_http.route("/article.html", html_response(ARTICLE))

    page_main_text(f"{stub_http.url}/article.html", user_agent="brief-test/9.9")

    assert stub_http.last("/article.html").headers["User-Agent"] == "brief-test/9.9"


def test_query_string_reaches_the_server_unchanged(stub_http):
    stub_http.route("/article.html?id=7&q=a%20b", html_response(ARTICLE))

    page_main_text(f"{stub_http.url}/article.html?id=7&q=a%20b")

    assert stub_http.requests[-1].path == "/article.html?id=7&q=a%20b"


def test_boilerplate_is_stripped_from_the_fetched_page(stub_http):
    stub_http.route("/article.html", html_response(ARTICLE))

    text = page_main_text(f"{stub_http.url}/article.html")["text"]

    assert "We use cookies" not in text
    assert "All rights reserved" not in text
    assert "Another story" not in text


# --------------------------------------------------------------- errors ---


def test_page_fetch_error_is_a_runtime_error():
    """The boundary promises a RuntimeError subclass; callers catch on it."""
    assert issubclass(PageFetchError, RuntimeError)

    error = PageFetchError("boom", url="https://example.invalid/a", status=418)
    assert isinstance(error, RuntimeError)
    assert (error.url, error.status) == ("https://example.invalid/a", 418)

    bare = PageFetchError("boom")
    assert bare.status is None


def test_non_200_status_raises_naming_url_and_status(stub_http):
    stub_http.route("/missing.html", html_response(b"<html>gone</html>", status=404))
    url = f"{stub_http.url}/missing.html"

    with pytest.raises(PageFetchError) as excinfo:
        page_main_text(url)

    assert url in str(excinfo.value)
    assert "404" in str(excinfo.value)
    assert excinfo.value.status == 404
    assert excinfo.value.url == url
    assert stub_http.requests_to("/missing.html"), "the request was actually attempted"


def test_server_error_status_raises(stub_http):
    stub_http.down = True
    stub_http.route("/article.html", html_response(ARTICLE))

    with pytest.raises(PageFetchError) as excinfo:
        page_main_text(f"{stub_http.url}/article.html")

    assert excinfo.value.status == 503


def test_connection_failure_raises_naming_the_url(unreachable_url):
    url = f"{unreachable_url}/article.html"

    with pytest.raises(PageFetchError) as excinfo:
        page_main_text(url, timeout_s=2.0)

    assert url in str(excinfo.value)
    assert excinfo.value.status is None


def test_timeout_raises_rather_than_hanging(stub_http):
    def slow(request):
        time.sleep(1.0)
        return html_response(ARTICLE)

    stub_http.route("/slow.html", slow)
    url = f"{stub_http.url}/slow.html"

    started = time.monotonic()
    with pytest.raises(PageFetchError) as excinfo:
        page_main_text(url, timeout_s=0.2)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "the timeout was not applied to the transport"
    assert url in str(excinfo.value)


def test_malformed_body_raises_rather_than_returning_empty_text(stub_http):
    """A body that is not a document at all — here JSON, and raw binary."""
    stub_http.route("/junk.json", html_response(b'{"error": "not found", "code": 404}'))
    stub_http.route("/junk.bin", html_response(bytes(range(32)) * 4))

    for path in ("/junk.json", "/junk.bin"):
        with pytest.raises(PageFetchError) as excinfo:
            page_main_text(f"{stub_http.url}{path}")
        assert "no main text" in str(excinfo.value).lower()


def test_truncated_html_still_yields_the_prose_it_contains(stub_http):
    """Tolerance, pinned: a body cut off mid-document is not an error."""
    stub_http.route(
        "/half.html",
        html_response(
            b"<html><head><title>Half a Page</title></head><body><article>"
            b"<p>The response was cut off by the proxy mid-document, but this "
            b"paragraph is intact and long enough to extract.</p>"
        ),
    )

    result = page_main_text(f"{stub_http.url}/half.html")

    assert result["title"] == "Half a Page"
    assert "cut off by the proxy" in result["text"]


def test_page_that_is_all_navigation_raises(stub_http):
    stub_http.route("/nav.html", html_response(BOILERPLATE))

    with pytest.raises(PageFetchError):
        page_main_text(f"{stub_http.url}/nav.html")


def test_non_http_scheme_is_refused_before_any_transport_call(tmp_path):
    page = tmp_path / "local.html"
    page.write_bytes(ARTICLE)
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        raise AssertionError("transport must not be reached")

    with pytest.raises(PageFetchError) as excinfo:
        page_main_text(page.as_uri(), open_url=transport)

    assert calls == []
    assert "scheme" in str(excinfo.value).lower()


# ------------------------------------------------------------ truncation ---


def test_max_chars_truncates_and_flags(stub_http):
    stub_http.route("/article.html", html_response(ARTICLE))

    result = page_main_text(f"{stub_http.url}/article.html", max_chars=40)

    assert len(result["text"]) == 40
    assert result["truncated"] is True


def test_max_chars_above_the_text_length_does_not_flag(stub_http):
    stub_http.route("/article.html", html_response(ARTICLE))

    uncapped = page_main_text(f"{stub_http.url}/article.html")
    capped = page_main_text(f"{stub_http.url}/article.html", max_chars=100_000)

    assert capped["text"] == uncapped["text"]
    assert capped["truncated"] is False


def test_max_chars_counts_characters_not_bytes():
    """A cap of 12 keeps 12 *characters*, even when each is several bytes."""
    html = (
        "<html><body><article><p>北京の記事、café naïve 🙂 — this paragraph is long "
        "enough that the extractor keeps it as real body content rather than "
        "discarding it as a caption.</p></article></body></html>"
    ).encode("utf-8")

    uncut = extract_main_text(html)["text"]
    assert uncut.startswith("北京")

    cut = extract_main_text(html, max_chars=12)["text"]

    assert len(cut) == 12
    assert cut == uncut[:12]
    assert len(cut.encode("utf-8")) > 12, "the cap was applied to bytes, not characters"
    assert "�" not in cut, "a multi-byte character was cut in half"


def test_max_chars_exactly_the_text_length_is_not_truncated():
    """The boundary, pinned: > the cap truncates, == the cap does not."""
    full = extract_main_text(ARTICLE)["text"]
    length = len(full)

    exact = extract_main_text(ARTICLE, max_chars=length)
    assert exact["truncated"] is False
    assert exact["text"] == full

    one_over = extract_main_text(ARTICLE, max_chars=length + 1)
    assert one_over["truncated"] is False
    assert one_over["text"] == full

    one_under = extract_main_text(ARTICLE, max_chars=length - 1)
    assert one_under["truncated"] is True
    assert one_under["text"] == full[: length - 1]


def test_max_chars_of_one_keeps_exactly_one_character():
    result = extract_main_text(ARTICLE, max_chars=1)

    assert len(result["text"]) == 1
    assert result["truncated"] is True


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_max_chars_is_a_caller_bug(bad):
    with pytest.raises(ValueError):
        extract_main_text(ARTICLE, max_chars=bad)


# -------------------------------------------------------- pure extraction ---


def test_extract_main_text_needs_no_socket():
    result = extract_main_text(ARTICLE, url="https://example.invalid/a")

    assert result["status"] == 200
    assert result["title"] == "Headline of the Article"
    assert FIRST_SENTENCE in result["text"]
    assert result["truncated"] is False


def test_extract_collapses_whitespace():
    """The fixture keeps what trafilatura keeps: a <pre> block's runs of
    spaces, a leading indent, a tab, and non-breaking spaces."""
    text = extract_main_text(WHITESPACE)["text"]

    assert "indented code block" in text, "runs of spaces were not collapsed"
    assert "A non-breaking space runs" in text, "U+00A0 was not collapsed"
    assert "Tail paragraph" in text
    assert "  " not in text
    assert "\t" not in text
    assert "\r" not in text
    assert "\xa0" not in text
    assert "\n\n" not in text
    assert not any(line != line.strip() for line in text.splitlines())
    assert text == text.strip()


def test_paragraph_boundaries_survive_as_single_newlines():
    """Collapsing whitespace must not weld paragraphs into one run-on line."""
    text = extract_main_text(ARTICLE)["text"]
    lines = text.splitlines()

    assert len(lines) >= 3, "paragraph boundaries were collapsed away"
    assert any(line.startswith(FIRST_SENTENCE) for line in lines)
    assert any(line.startswith("A second paragraph") for line in lines)
    assert not any(
        FIRST_SENTENCE in line and "A second paragraph" in line for line in lines
    ), "two paragraphs ended up on one line"
    assert "\n\n" not in text


def test_extract_preserves_non_ascii():
    text = extract_main_text(ARTICLE)["text"]

    assert "café" in text
    assert "naïve" in text
    assert "北京" in text
    assert "🙂" in text


def test_extract_honours_a_declared_non_utf8_charset():
    result = extract_main_text(LATIN1, url="https://example.invalid/fr")

    assert "café français" in result["text"]
    assert "Ã©" not in result["text"], "latin-1 bytes were decoded as utf-8"


def test_extract_on_empty_bytes_raises():
    with pytest.raises(PageFetchError):
        extract_main_text(b"", url="https://example.invalid/empty")


def test_extract_on_boilerplate_only_raises_and_names_the_url():
    with pytest.raises(PageFetchError) as excinfo:
        extract_main_text(BOILERPLATE, url="https://example.invalid/nav")

    assert "https://example.invalid/nav" in str(excinfo.value)


def test_missing_title_is_none_not_empty_string():
    html = (
        b"<html><body><article><p>A body paragraph long enough that the extractor "
        b"keeps it as main content, with no title element anywhere on the page.</p>"
        b"</article></body></html>"
    )

    result = extract_main_text(html)

    assert result["title"] is None
    assert "A body paragraph" in result["text"]


def test_blank_title_becomes_none():
    def blank_metadata(payload):
        class Meta:
            title = "   "

        return Meta()

    assert _title_from(b"<html></html>", blank_metadata) is None


def test_a_failing_metadata_extractor_costs_the_title_not_the_text():
    """Fail-open, pinned: the title is the only best-effort field."""

    def exploding_metadata(payload):
        raise ValueError("metadata parser exploded")

    assert _title_from(ARTICLE, exploding_metadata) is None

    def working_metadata(payload):
        class Meta:
            title = " Recovered Title "

        return Meta()

    assert _title_from(ARTICLE, working_metadata) == "Recovered Title"


# ------------------------------------------------------------- transport ---


def test_open_url_seam_receives_url_headers_timeout(stub_http):
    seen = []

    def transport(url, headers, timeout):
        seen.append((url, dict(headers), timeout))
        return 200, {"Content-Type": "text/html"}, ARTICLE

    result = page_main_text(
        "https://example.invalid/a", timeout_s=7.5, open_url=transport
    )

    assert result["title"] == "Headline of the Article"
    assert len(seen) == 1
    url, headers, timeout = seen[0]
    assert url == "https://example.invalid/a"
    assert headers["User-Agent"] == DEFAULT_USER_AGENT
    assert timeout == 7.5
    assert stub_http.requests == [], "the default transport was used anyway"


def test_open_url_seam_is_called_positionally():
    def transport(a, b, c):  # deliberately unrelated parameter names
        return 200, {}, ARTICLE

    assert (
        page_main_text("https://example.invalid/a", open_url=transport)["status"] == 200
    )


def test_open_url_status_other_than_200_raises():
    def transport(url, headers, timeout):
        return 301, {"Location": "https://elsewhere.invalid/"}, b""

    with pytest.raises(PageFetchError) as excinfo:
        page_main_text("https://example.invalid/a", open_url=transport)

    assert excinfo.value.status == 301


def test_open_url_failure_is_wrapped_as_page_fetch_error():
    def transport(url, headers, timeout):
        raise OSError("transport exploded")

    with pytest.raises(PageFetchError) as excinfo:
        page_main_text("https://example.invalid/a", open_url=transport)

    assert "transport exploded" in str(excinfo.value)
    assert "https://example.invalid/a" in str(excinfo.value)


# ------------------------------------------------------------ lazy import ---


def test_trafilatura_is_not_imported_at_module_import():
    probe = (
        "import sys; import brief.lib.page_main_text as m; "
        "print('trafilatura' in sys.modules)"
    )
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=root,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin"},
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"


def test_module_imports_nothing_from_the_app():
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "brief"
        / "lib"
        / "page_main_text.py"
    ).read_text()

    assert "from brief" not in source
    assert "import brief" not in source
