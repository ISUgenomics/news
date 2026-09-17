"""A bot wall must be named, not debugged.

`hard-stop-on-bot-detection`, from knowledge_graph BEST_PRACTICES #25 by way
of `codeLibrary/PRACTICES.md`. Half of it was already true here: nothing in
this repo retries or rotates headers, so a blocked fetch already stopped. The
missing half was the message — a wall surfaced as "no parseable entries" or
"returned HTTP 403", which sends the operator to look for a broken URL or a
broken parser.

THE DESIGN CONSTRAINT IS THE FALSE POSITIVE. The obvious implementation is
knowledge_graph's: search the body for "captcha", "cloudflare", "challenge".
That is wrong for this project specifically, and the corpus says so — across
1,360 stored items, "captcha" and "cloudflare" appear in 0, and "challenge"
appears in 267. A profile on web security would hit the others. Refusing one
real article in five is how a control gets switched off.

So every signal is structural: a response header the page cannot fake through
its content, the exact `<title>` of a known interstitial, or a `src=`/`action=`
pointing at a challenge provider. An article *about* Cloudflare carries the
word in prose, never in a script source.

The detector is duplicated in feed_fetch and page_main_text because seeds
graduate individually and may not import one another; the last test here pins
the copies together.
"""

from __future__ import annotations

import pytest

from brief.lib import feed_fetch, page_main_text
from harness.stub_http import Response
from harness.stub_http import stub_http as stub_http_server  # noqa: F401

DETECTORS = [
    pytest.param(feed_fetch.detect_block, id="feed_fetch"),
    pytest.param(page_main_text.detect_block, id="page_main_text"),
]

ARTICLE_ABOUT_BOT_WALLS = (
    b"<html><head><title>ISU team studies how Cloudflare and reCAPTCHA "
    b"challenge automated traffic</title></head><body><p>The researchers "
    b"examined captcha challenge platforms and bot detection at scale, "
    b"checking your browser is a human.</p></body></html>"
)

CHALLENGE_PAGE = (
    b"<html><head><title>Just a moment...</title></head><body>"
    b'<script src="https://challenges.cloudflare.com/turnstile/v0/api.js">'
    b"</script></body></html>"
)

FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title>
<item><title>Real item</title><link>https://example.test/a</link><guid>a</guid>
</item></channel></rss>"""


# ------------------------------------------------ the false positive first ---


@pytest.mark.parametrize("detect", DETECTORS)
def test_an_article_about_bot_walls_is_not_a_bot_wall(detect):
    """The test this detector exists to pass. A naive body search fails it,
    and a detector that refuses real articles gets disabled within a week."""
    assert detect(200, {}, ARTICLE_ABOUT_BOT_WALLS) is None


@pytest.mark.parametrize("detect", DETECTORS)
def test_the_word_challenge_in_prose_is_not_a_signal(detect):
    """267 of 1,360 stored items contain it — grant challenges, Grand
    Challenges, challenge awards."""
    body = (
        b"<html><head><title>Grand Challenge award to ISU</title></head>"
        b"<body><p>A challenge grant supports the work.</p></body></html>"
    )
    assert detect(200, {}, body) is None


@pytest.mark.parametrize("detect", DETECTORS)
def test_an_ordinary_404_is_not_a_bot_wall(detect):
    assert detect(404, {"Server": "nginx"}, b"<title>Not Found</title>") is None


@pytest.mark.parametrize("detect", DETECTORS)
def test_a_cloudflare_served_page_that_is_not_blocking_passes(detect):
    """Plenty of real sites sit behind Cloudflare. Serving is not blocking —
    only a blocking status plus the edge counts."""
    assert detect(200, {"Server": "cloudflare"}, b"<title>Real news</title>") is None


# ------------------------------------------------------- the real signals ---


@pytest.mark.parametrize("detect", DETECTORS)
@pytest.mark.parametrize(
    "status,headers,body,expected",
    [
        (200, {"cf-mitigated": "challenge"}, b"<html></html>", "cf-mitigated"),
        (403, {"Server": "cloudflare"}, b"<html></html>", "Cloudflare block"),
        (429, {"Server": "cloudflare"}, b"<html></html>", "Cloudflare block"),
        (200, {}, CHALLENGE_PAGE, "just a moment"),
        (
            200,
            {},
            b'<html><body><script src="https://www.google.com/recaptcha/api.js">'
            b"</script></body></html>",
            "recaptcha",
        ),
        (
            200,
            {},
            b'<html><body><form action="https://hcaptcha.com/verify"></form></body></html>',
            "hcaptcha",
        ),
        (
            200,
            {},
            b"<html><head><title>Attention Required! | Cloudflare</title></head></html>",
            "attention required",
        ),
    ],
)
def test_a_real_wall_is_named(detect, status, headers, body, expected):
    reason = detect(status, headers, body)
    assert reason is not None
    assert expected.lower() in reason.lower()


# -------------------------------------------------- through the real seeds ---


def test_fetch_feed_reports_the_wall_not_a_parser_failure(stub_http_server):
    """Through fetch_feed(). Before this, a walled feed raised 'no parseable
    entries in the feed', which is a true statement about the wrong thing."""
    stub_http_server.route("/feed.xml", CHALLENGE_PAGE)

    with pytest.raises(feed_fetch.FeedBlockedError) as caught:
        feed_fetch.fetch_feed(f"{stub_http_server.url}/feed.xml")

    message = str(caught.value)
    assert "bot wall" in message
    assert "Not retried" in message
    assert "parseable entries" not in message


def test_page_main_text_reports_the_wall_before_the_status_check(stub_http_server):
    """A wall answers 403 as readily as 200. 'returned HTTP 403, not 200'
    sends the operator hunting a broken URL."""
    stub_http_server.route(
        "/a",
        Response(CHALLENGE_PAGE, status=403, headers={"Server": "cloudflare"}),
    )

    with pytest.raises(page_main_text.PageBlockedError) as caught:
        page_main_text.page_main_text(f"{stub_http_server.url}/a")

    assert "bot wall" in str(caught.value)
    assert "not 200" not in str(caught.value)


def test_a_blocked_error_is_still_catchable_as_the_old_error_type(stub_http_server):
    """Subclassed on purpose: every existing handler keeps working."""
    stub_http_server.route("/feed.xml", CHALLENGE_PAGE)

    with pytest.raises(feed_fetch.FeedFetchError):
        feed_fetch.fetch_feed(f"{stub_http_server.url}/feed.xml")


def test_a_healthy_feed_is_unaffected(stub_http_server):
    stub_http_server.route("/feed.xml", FEED)

    result = feed_fetch.fetch_feed(f"{stub_http_server.url}/feed.xml")

    assert [e["title"] for e in result["entries"]] == ["Real item"]


def test_nothing_retries_a_blocked_fetch(stub_http_server):
    """The other half of the practice. One request, then stop — rotation
    never defeats real bot detection and risks the IP."""
    stub_http_server.route("/feed.xml", CHALLENGE_PAGE)

    with pytest.raises(feed_fetch.FeedBlockedError):
        feed_fetch.fetch_feed(f"{stub_http_server.url}/feed.xml")

    assert len(stub_http_server.requests_to("/feed.xml")) == 1


# ------------------------------------------------------------- duplication ---


def test_the_two_copies_of_the_detector_agree():
    """They are duplicated because seeds may not import each other. That is
    only safe while they behave identically."""
    assert feed_fetch._BLOCK_TITLES == page_main_text._BLOCK_TITLES
    assert (
        feed_fetch._CHALLENGE_SRC.pattern == page_main_text._CHALLENGE_SRC.pattern
    )
