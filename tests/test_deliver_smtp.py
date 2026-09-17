"""Delivery against a real SMTP server on a real socket.

`tests/test_pipeline.py` covers the ordering and idempotency rules with the
send call stubbed out. These go the rest of the way: an actual socket, an
actual envelope, so the parts a substituted callable cannot exercise — MAIL
FROM and RCPT TO, header folding, the multipart structure, a per-recipient
refusal, a server that is down — are covered by something other than trust.
"""

from __future__ import annotations

import email
import email.header
import email.policy
from datetime import datetime, timezone

import pytest

from brief.deliver import AlreadySent, deliver
from brief.models import Bucket, Delivery, Profile, Relevance, SourceRef
from harness.stub_smtp import stub_smtp  # noqa: F401  (pytest fixture)

NOW = datetime(2026, 9, 17, 6, 30, tzinfo=timezone.utc)
WEEK = "2026-09-14"

BRIEF = """# Test Brief

week of 2026-09-14

## Funding

- A university received $1,234,567 from a sponsor. [1](https://example.test/award)

---

*Prepared automatically from public sources; every statement links to its source.*
"""


def make_profile(**over) -> Profile:
    base = dict(
        name="test",
        title="Test Brief",
        audience="a reader",
        persona="an analyst",
        sources=(SourceRef("feed_a"),),
        relevance=Relevance(any_of=("thing",)),
        buckets=(Bucket("Funding", "awards"),),
        delivery=Delivery(sender="brief@example.test", to=("reader@example.test",)),
    )
    base.update(over)
    return Profile(**base)


def send(stub, profile, tmp_path, **over):
    kwargs = dict(
        week_start=WEEK,
        item_count=1,
        briefs_root=tmp_path / "briefs",
        smtp={"host": stub.host, "port": stub.port},
        sent_at=None,
        now=NOW,
    )
    kwargs.update(over)
    return deliver(BRIEF, profile, **kwargs)


def test_the_envelope_carries_the_profiles_sender_and_recipients(stub_smtp, tmp_path):
    profile = make_profile(
        delivery=Delivery(
            sender="brief@example.test", to=("one@example.test", "two@example.test")
        )
    )
    send(stub_smtp, profile, tmp_path)

    envelope = stub_smtp.last()
    assert envelope.mail_from == "brief@example.test"
    assert sorted(envelope.rcpt_tos) == ["one@example.test", "two@example.test"]


def test_the_message_is_multipart_with_markdown_as_the_text_alternative(
    stub_smtp, tmp_path
):
    send(stub_smtp, make_profile(), tmp_path)
    message = email.message_from_bytes(stub_smtp.last().data, policy=email.policy.default)

    assert message.get_content_type() == "multipart/alternative"
    kinds = [
        p.get_content_type()
        for p in message.walk()
        if p.get_content_maintype() == "text"
    ]
    assert kinds == ["text/plain", "text/html"], (
        "plain first, so a text reader picks it"
    )

    plain = message.get_body(preferencelist=("plain",)).get_content()
    assert "$1,234,567" in plain
    assert "# Test Brief" in plain, "the plain part is the Markdown itself"


def test_the_html_part_keeps_the_links_that_make_a_claim_checkable(stub_smtp, tmp_path):
    send(stub_smtp, make_profile(), tmp_path)
    message = email.message_from_bytes(stub_smtp.last().data, policy=email.policy.default)
    html = message.get_body(preferencelist=("html",)).get_content()

    assert 'href="https://example.test/award"' in html
    assert "<style" in html, "an inline stylesheet, since no mail client fetches one"


def test_the_subject_is_the_profile_template_expanded(stub_smtp, tmp_path):
    send(stub_smtp, make_profile(), tmp_path, item_count=42)
    message = email.message_from_bytes(stub_smtp.last().data, policy=email.policy.default)
    assert str(message["Subject"]) == "Test Brief — week of 2026-09-14 (42 items)"


def test_a_unicode_subject_survives_the_wire(stub_smtp, tmp_path):
    profile = make_profile(title="Brief — ünïcode ✓")
    send(stub_smtp, profile, tmp_path)
    message = email.message_from_bytes(stub_smtp.last().data, policy=email.policy.default)
    assert "ünïcode ✓" in str(message["Subject"])


def test_a_refused_recipient_is_reported_and_the_others_still_receive_it(
    stub_smtp, tmp_path
):
    stub_smtp.refuse["blocked@example.test"] = (550, b"No such user")
    profile = make_profile(
        delivery=Delivery(
            sender="brief@example.test",
            to=("good@example.test", "blocked@example.test"),
        )
    )
    result = send(stub_smtp, profile, tmp_path)

    assert "blocked@example.test" in result.refused
    assert stub_smtp.last().rcpt_tos == ["good@example.test"]


def test_the_archive_is_written_before_the_send_so_a_failure_is_recoverable(
    stub_smtp, tmp_path
):
    stub_smtp.down = True
    with pytest.raises(Exception):
        send(stub_smtp, make_profile(), tmp_path)

    archived = tmp_path / "briefs" / "test" / f"{WEEK}.md"
    assert archived.exists(), "a brief that could not be sent must still be on disk"
    assert archived.read_text(encoding="utf-8") == BRIEF


def test_an_already_sent_week_never_opens_a_socket(stub_smtp, tmp_path):
    before = len(stub_smtp.envelopes)
    with pytest.raises(AlreadySent):
        send(stub_smtp, make_profile(), tmp_path, sent_at="2026-09-14T06:30:00Z")
    assert len(stub_smtp.envelopes) == before


def test_resend_does_reach_the_server(stub_smtp, tmp_path):
    send(
        stub_smtp, make_profile(), tmp_path, sent_at="2026-09-14T06:30:00Z", resend=True
    )
    assert stub_smtp.last().rcpt_tos == ["reader@example.test"]
