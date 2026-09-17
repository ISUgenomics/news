"""Every seed that fetches must undo `Content-Encoding` before decoding.

`urllib` does not decompress and `requests` does, so this is the gotcha that
survives a code review: `.read().decode()` on a gzip body returns replacement
characters rather than raising, and the failure surfaces far downstream as
nonsense content. Learned in knowledge_graph
(`docs/BEST_PRACTICES.md#26`), reached here through
`codeLibrary/PRACTICES.md` as `urllib-does-not-decompress`.

Nothing here sends `Accept-Encoding`, deliberately — that would invite
compression on six live sources rather than defuse a trap. Measured against
the real endpoints before writing this: today they all answer uncompressed,
but PubMed returns gzip the moment anything asks for it. So the guard is for
the proxy, the CDN, and whoever adds a header next.

Real sockets, real gzip bytes, through each seed's public entry point — a
test that only exercised the helper would pass while a caller skipped it,
which is the failure mode this repo keeps meeting.

This file is cross-cutting, which is the one thing to remember at graduation:
each seed keeps its own sibling test, but its compression case lives here. A
seed lifted into codeLibrary on its own would leave that coverage behind, so
copy the relevant cases across with it.
"""

from __future__ import annotations

import ast
import gzip
import json
import zlib
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from brief.lib import feed_fetch, pubmed_search
from harness.stub_http import Response
from harness.stub_http import stub_http as stub_http_server  # noqa: F401

LIB = Path(__file__).resolve().parents[1] / "src" / "brief" / "lib"

FETCHING_SEEDS = [
    "feed_fetch.py",
    "page_main_text.py",
    "nsf_award_search.py",
    "usaspending_award_search.py",
    "nih_reporter_search.py",
    "pubmed_search.py",
]

class _PathOnlyRoutes(dict):
    """Route on the path, ignoring the query string.

    `stub_http` keys routes on the raw request target. E-utilities puts every
    parameter in the query, so an exact-target route would mean spelling out
    the encoding under test. Only the lookup changes; the server, the socket
    and the recorded requests stay the harness's own. Same device as
    tests/test_pubmed_search.py.
    """

    def __contains__(self, key) -> bool:
        method, target = key
        return dict.__contains__(self, (method, urlsplit(target).path))

    def __getitem__(self, key):
        method, target = key
        return dict.__getitem__(self, (method, urlsplit(target).path))


@pytest.fixture
def path_routed_stub(stub_http_server):
    stub_http_server.routes = _PathOnlyRoutes()
    return stub_http_server


FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title>
<item><title>Compressed item</title><link>https://example.test/a</link>
<guid>a</guid><pubDate>Tue, 16 Sep 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""


# --------------------------------------------------- through a real seed ---


def test_a_gzip_feed_body_reaches_the_parser_intact(stub_http_server):
    """Through fetch_feed(), not the helper: the whole point is that the
    caller routes its read through the guard."""
    stub_http_server.route(
        "/feed.xml",
        Response(gzip.compress(FEED), headers={"Content-Encoding": "gzip"}),
    )

    result = feed_fetch.fetch_feed(f"{stub_http_server.url}/feed.xml")

    assert result["status"] == 200
    assert [e["title"] for e in result["entries"]] == ["Compressed item"]


def test_a_deflate_feed_body_reaches_the_parser_intact(stub_http_server):
    stub_http_server.route(
        "/feed.xml",
        Response(zlib.compress(FEED), headers={"Content-Encoding": "deflate"}),
    )

    result = feed_fetch.fetch_feed(f"{stub_http_server.url}/feed.xml")

    assert [e["title"] for e in result["entries"]] == ["Compressed item"]


def test_raw_deflate_without_the_zlib_wrapper_also_works(stub_http_server):
    """Some servers send raw deflate. zlib.decompress rejects it, and the
    fallback with a negative window size is the documented way through."""
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw = compressor.compress(FEED) + compressor.flush()
    stub_http_server.route(
        "/feed.xml", Response(raw, headers={"Content-Encoding": "deflate"})
    )

    result = feed_fetch.fetch_feed(f"{stub_http_server.url}/feed.xml")

    assert [e["title"] for e in result["entries"]] == ["Compressed item"]


def test_an_uncompressed_body_is_untouched(stub_http_server):
    """The path every source actually takes today must not change."""
    stub_http_server.route("/feed.xml", FEED)

    result = feed_fetch.fetch_feed(f"{stub_http_server.url}/feed.xml")

    assert [e["title"] for e in result["entries"]] == ["Compressed item"]


def test_identity_encoding_is_not_treated_as_compression(stub_http_server):
    stub_http_server.route(
        "/feed.xml", Response(FEED, headers={"Content-Encoding": "identity"})
    )

    result = feed_fetch.fetch_feed(f"{stub_http_server.url}/feed.xml")

    assert [e["title"] for e in result["entries"]] == ["Compressed item"]


def test_a_gzip_json_body_reaches_the_json_parser(path_routed_stub):
    """A different seed and a different body handling: pubmed decodes to
    text and parses JSON, where feed_fetch hands bytes to the parser."""
    payload = json.dumps(
        {"esearchresult": {"idlist": ["111", "222"], "count": "2"}}
    ).encode()
    path_routed_stub.route(
        "/entrez/eutils/esearch.fcgi",
        Response(gzip.compress(payload), headers={"Content-Encoding": "gzip"}),
    )

    ids = pubmed_search.esearch_ids(
        "maize",
        email="t@example.test",
        base_url=f"{path_routed_stub.url}/entrez/eutils",
    )

    assert ids == ["111", "222"]


def test_a_lying_content_encoding_raises_rather_than_yielding_garbage(
    stub_http_server,
):
    """Returning the undecodable bytes is the silent-garbage path this guard
    exists to remove. Ingest catches per source and reports `source.error`,
    so a loud failure costs one source, not the run."""
    stub_http_server.route(
        "/feed.xml", Response(FEED, headers={"Content-Encoding": "gzip"})
    )

    with pytest.raises(Exception) as caught:
        feed_fetch.fetch_feed(f"{stub_http_server.url}/feed.xml")

    assert "Compressed item" not in str(caught.value)


def test_an_unknown_encoding_is_refused_not_guessed(stub_http_server):
    stub_http_server.route(
        "/feed.xml", Response(FEED, headers={"Content-Encoding": "br"})
    )

    with pytest.raises(Exception):
        feed_fetch.fetch_feed(f"{stub_http_server.url}/feed.xml")


# ------------------------------------------------------------ structural ---


@pytest.mark.parametrize("name", FETCHING_SEEDS)
def test_no_seed_reads_a_response_body_without_decompressing(name):
    """The guard that scales to the next seed.

    Each of these modules fetches, and each had to be edited by hand. A new
    one — or a new call site in an existing one — would silently reintroduce
    the bug, and no behavioural test would notice because the servers do not
    compress today. So: every `.read()` on a response must be wrapped.
    """
    tree = ast.parse((LIB / name).read_text(encoding="utf-8"))

    bare: list[int] = []
    wrapped: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_decompressed"
        ):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    wrapped.add(id(inner))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "read"
            and id(node) not in wrapped
        ):
            bare.append(node.lineno)

    assert not bare, (
        f"{name}: response .read() not routed through _decompressed at "
        f"line(s) {bare}"
    )


@pytest.mark.parametrize("name", FETCHING_SEEDS)
def test_every_fetching_seed_defines_the_guard_itself(name):
    """Seeds graduate individually and may not import each other, so the
    helper is duplicated on purpose. Duplicated means each copy has to be
    present — this is what makes the duplication safe rather than lucky."""
    source = (LIB / name).read_text(encoding="utf-8")
    assert "def _decompressed(" in source
