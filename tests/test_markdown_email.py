"""Tests for the markdown-email seed.

One test per documented behavior in docs/seeds/markdown-email.md, plus the edge cases
named there: empty recipients, blank sender, refused recipients, an unreachable relay,
unicode, an empty document, a wholesale stylesheet replacement, header override, and
the lazy `markdown` import — plus the two the review added: a line break in any value
that becomes a header is refused (header injection), and the exact sense in which the
text/plain part is "verbatim". Sending runs against the threaded stub SMTP server in
tests/harness/stub_smtp.py — a real socket on an ephemeral port, never the network.
"""

from __future__ import annotations

import email
import os
import pathlib
import smtplib
import subprocess
import sys
import textwrap
from email import policy
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest

from brief.lib.markdown_email import (
    DEFAULT_STYLESHEET,
    build_message,
    markdown_to_html,
    send_markdown_email,
    send_message,
)
from harness.stub_smtp import stub_smtp, unreachable_smtp  # noqa: F401  (fixtures)

MARKDOWN = textwrap.dedent(
    """\
    # Weekly brief

    Funding moved this week.

    - [An award](https://example.test/award) landed.
    - A paper appeared.
    """
)

SENDER = "brief@example.test"
RECIPIENTS = ["reader@example.test", "other@example.test"]
WHEN = datetime(2026, 3, 2, 9, 30, tzinfo=timezone.utc)
MSGID = "<brief-2026-03-02@example.test>"


def _message(**overrides) -> EmailMessage:
    markdown_text = overrides.pop("markdown_text", MARKDOWN)
    kwargs = dict(
        subject="Weekly brief — week of 2026-03-02",
        sender=SENDER,
        recipients=RECIPIENTS,
        date=WHEN,
        msgid=MSGID,
    )
    kwargs.update(overrides)
    return build_message(markdown_text, **kwargs)


def _parts(message: EmailMessage) -> dict[str, str]:
    return {
        part.get_content_type(): part.get_content()
        for part in message.walk()
        if not part.is_multipart()
    }


# --- markdown_to_html -------------------------------------------------------


def test_markdown_to_html_returns_a_complete_document_with_the_stylesheet_in_head():
    html = markdown_to_html(MARKDOWN, stylesheet="body { color: rebeccapurple; }")

    assert html.startswith("<!DOCTYPE html>")
    head = html[: html.index("</head>")]
    assert "<style>" in head
    assert "body { color: rebeccapurple; }" in head
    assert html.rstrip().endswith("</html>")


def test_markdown_to_html_converts_headings_lists_and_links():
    html = markdown_to_html(MARKDOWN)

    assert "<h1>Weekly brief</h1>" in html
    assert '<a href="https://example.test/award">An award</a>' in html
    assert "<ul>" in html and "<li>" in html


def test_markdown_to_html_renders_tables_from_the_extra_extension():
    html = markdown_to_html("| a | b |\n| - | - |\n| 1 | 2 |\n")

    assert "<table>" in html
    assert "<td>1</td>" in html


def test_markdown_to_html_replaces_the_default_stylesheet_wholesale():
    html = markdown_to_html(MARKDOWN, stylesheet="p{color:red}")

    assert html.count("<style>") == 1
    assert "p{color:red}" in html
    # nothing of the default survives alongside it
    assert DEFAULT_STYLESHEET.strip() not in html


def test_markdown_to_html_uses_the_default_stylesheet_when_none_is_given():
    html = markdown_to_html(MARKDOWN)

    assert DEFAULT_STYLESHEET.strip()
    assert DEFAULT_STYLESHEET.strip() in html


def test_markdown_to_html_escapes_the_title_and_omits_it_when_empty():
    titled = markdown_to_html(MARKDOWN, title='AI & "policy" <brief>')
    untitled = markdown_to_html(MARKDOWN)

    assert "<title>AI &amp; &quot;policy&quot; &lt;brief&gt;</title>" in titled
    assert "<title>" not in untitled


def test_markdown_to_html_keeps_unicode_as_characters_and_declares_utf8():
    html = markdown_to_html("Grant för Ångström — 研究 ✓\n")

    assert "Grant för Ångström — 研究 ✓" in html
    assert 'charset="utf-8"' in html.lower()


def test_markdown_to_html_of_an_empty_document_is_still_a_document():
    html = markdown_to_html("")

    assert html.startswith("<!DOCTYPE html>")
    assert "<body>" in html and "</body>" in html


def test_markdown_is_imported_lazily_so_the_module_imports_without_it():
    script = textwrap.dedent(
        """
        import sys
        sys.modules["markdown"] = None   # make `import markdown` raise ImportError
        from brief.lib.markdown_email import markdown_to_html, build_message
        try:
            markdown_to_html("hi")
        except ImportError:
            print("lazy")
        else:
            raise SystemExit("markdown_to_html did not import markdown at call time")
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(pathlib.Path(__file__).resolve().parents[1]),
        env={**os.environ, "PYTHONPATH": "src:tests"},
    )
    assert done.returncode == 0, done.stderr
    assert "lazy" in done.stdout


# --- build_message ----------------------------------------------------------


def test_build_message_carries_the_markdown_verbatim_as_the_plain_part():
    parts = _parts(_message())

    assert parts["text/plain"] == MARKDOWN


def test_build_message_adds_the_rendered_html_as_the_alternative():
    message = _message()

    assert message.get_content_type() == "multipart/alternative"
    html = _parts(message)["text/html"]
    assert "<h1>Weekly brief</h1>" in html
    assert '<a href="https://example.test/award">An award</a>' in html


def test_build_message_sets_subject_from_to_date_and_message_id():
    message = _message()

    assert message["Subject"] == "Weekly brief — week of 2026-03-02"
    assert message["From"] == SENDER
    assert message["To"] == "reader@example.test, other@example.test"
    assert message["Message-ID"] == MSGID
    assert "2 Mar 2026 09:30:00 +0000" in message["Date"]


def test_build_message_formats_a_supplied_date_in_its_own_offset():
    message = _message(date=WHEN.astimezone(timezone(timedelta(hours=-6))))

    assert "2 Mar 2026 03:30:00 -0600" in message["Date"]


def test_build_message_derives_the_message_id_domain_from_the_sender():
    message = build_message(
        MARKDOWN,
        subject="s",
        sender="Brief <brief@example.test>",
        recipients=RECIPIENTS,
    )

    assert message["Message-ID"].endswith("@example.test>")


def test_build_message_falls_back_to_localhost_when_the_sender_has_no_domain():
    message = build_message(
        MARKDOWN, subject="s", sender="brief", recipients=RECIPIENTS
    )

    assert message["Message-ID"].endswith("@localhost>")


def test_build_message_is_deterministic_when_date_and_msgid_are_given():
    first = _message().as_bytes()
    second = _message().as_bytes()

    assert first == second


def test_build_message_applies_extra_headers():
    message = _message(
        extra_headers={"Reply-To": "vpr@example.test", "List-Id": "<b.example.test>"}
    )

    assert message["Reply-To"] == "vpr@example.test"
    assert message["List-Id"] == "<b.example.test>"


def test_extra_headers_replace_rather_than_duplicate_an_existing_header():
    message = _message(extra_headers={"Subject": "Overridden"})

    assert message.get_all("Subject") == ["Overridden"]


def test_build_message_round_trips_unicode_through_serialization():
    text = "Grant för Ångström — 研究 ✓\n"
    message = _message(markdown_text=text)

    reparsed = email.message_from_bytes(message.as_bytes(), policy=policy.default)
    parts = {
        p.get_content_type(): p.get_content()
        for p in reparsed.walk()
        if not p.is_multipart()
    }
    assert parts["text/plain"] == text
    assert "研究" in parts["text/html"]
    assert message.as_bytes().isascii()  # transfer-encoded, safe for a 7-bit relay


def test_build_message_rejects_an_empty_recipient_list():
    with pytest.raises(ValueError, match="recipient"):
        build_message(MARKDOWN, subject="s", sender=SENDER, recipients=[])


def test_build_message_rejects_a_blank_sender():
    with pytest.raises(ValueError, match="sender"):
        build_message(MARKDOWN, subject="s", sender="   ", recipients=RECIPIENTS)


def test_build_message_rejects_a_blank_recipient_entry():
    with pytest.raises(ValueError, match="recipient"):
        build_message(
            MARKDOWN, subject="s", sender=SENDER, recipients=["a@example.test", " "]
        )


def test_build_message_accepts_any_sequence_of_recipients():
    message = build_message(
        MARKDOWN, subject="s", sender=SENDER, recipients=("one@example.test",)
    )

    assert message["To"] == "one@example.test"


def test_build_message_renders_the_html_part_with_the_stylesheet_it_was_given():
    message = _message(stylesheet="p{color:rebeccapurple}")

    html = _parts(message)["text/html"]
    assert "p{color:rebeccapurple}" in html
    assert DEFAULT_STYLESHEET.strip() not in html


def test_build_message_uses_the_subject_as_the_html_title():
    message = _message(subject="AI & <policy>")

    assert "<title>AI &amp; &lt;policy&gt;</title>" in _parts(message)["text/html"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sender": "brief@example.test\nBcc: evil@attacker.test"},
        {"recipients": ["reader@example.test\r\nBcc: evil@attacker.test"]},
        {"subject": "Weekly brief\nX-Injected: yes"},
        {"extra_headers": {"Reply-To": "vpr@example.test\nBcc: evil@attacker.test"}},
    ],
    ids=["sender", "recipient", "subject", "extra-header"],
)
def test_build_message_refuses_a_line_break_in_any_header_value(kwargs):
    with pytest.raises(ValueError, match="carriage return or linefeed"):
        _message(**kwargs)


def test_send_markdown_email_refuses_header_injection_before_connecting(
    unreachable_smtp,
):
    host, port = unreachable_smtp

    with pytest.raises(ValueError, match="carriage return or linefeed"):
        send_markdown_email(
            MARKDOWN,
            subject="s",
            sender=SENDER,
            recipients=["reader@example.test\nBcc: evil@attacker.test"],
            host=host,
            port=port,
        )


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("# Hi\n\nbody", "# Hi\n\nbody\n"),  # a trailing newline is added
        ("a\r\nb\r\n", "a\nb\n"),  # CRLF becomes LF
        ("", "\n"),  # an empty document is a single newline
        ("a line   \nb\n", "a line   \nb\n"),  # trailing spaces survive QP
        ("\n\n# Hi\n\n\n", "\n\n# Hi\n\n\n"),  # surrounding blank lines are content
    ],
    ids=["no-trailing-newline", "crlf", "empty", "trailing-spaces", "blank-lines"],
)
def test_build_message_plain_part_is_verbatim_up_to_line_ending_normalization(
    given, expected
):
    message = _message(markdown_text=given)

    assert _parts(message)["text/plain"] == expected
    reparsed = email.message_from_bytes(message.as_bytes(), policy=policy.default)
    delivered = {
        part.get_content_type(): part.get_content()
        for part in reparsed.walk()
        if not part.is_multipart()
    }
    assert delivered["text/plain"].replace("\r\n", "\n") == expected


# --- send_message -----------------------------------------------------------


def test_send_message_delivers_the_envelope_and_the_body(stub_smtp):
    refused = send_message(_message(), host=stub_smtp.host, port=stub_smtp.port)

    assert refused == {}
    envelope = stub_smtp.last()
    assert envelope.mail_from == SENDER
    assert envelope.rcpt_tos == RECIPIENTS
    assert "Subject: Weekly brief" in envelope.text
    assert envelope.message.get_content_type() == "multipart/alternative"


def test_send_message_quits_cleanly(stub_smtp):
    send_message(_message(), host=stub_smtp.host, port=stub_smtp.port)

    assert "QUIT" in stub_smtp.commands


def test_send_message_returns_the_recipients_the_relay_refused(stub_smtp):
    stub_smtp.refuse["other@example.test"] = (550, b"No such user")

    refused = send_message(_message(), host=stub_smtp.host, port=stub_smtp.port)

    assert refused == {"other@example.test": (550, b"No such user")}
    assert stub_smtp.last().rcpt_tos == ["reader@example.test"]


def test_send_message_propagates_a_total_refusal(stub_smtp):
    for address in RECIPIENTS:
        stub_smtp.refuse[address] = (550, b"No such user")

    with pytest.raises(smtplib.SMTPRecipientsRefused):
        send_message(_message(), host=stub_smtp.host, port=stub_smtp.port)

    assert stub_smtp.envelopes == []


def test_send_message_propagates_a_connection_failure(unreachable_smtp):
    host, port = unreachable_smtp

    with pytest.raises(OSError):
        send_message(_message(), host=host, port=port, timeout=2.0)


def test_send_message_propagates_an_smtp_error_from_the_relay(stub_smtp):
    stub_smtp.down = True

    with pytest.raises(smtplib.SMTPException):
        send_message(_message(), host=stub_smtp.host, port=stub_smtp.port)


def test_send_message_authenticates_when_a_username_is_given(stub_smtp):
    stub_smtp.advertise_auth = True

    send_message(
        _message(),
        host=stub_smtp.host,
        port=stub_smtp.port,
        username="relay-user",
        password="relay-pass",
    )

    assert stub_smtp.credentials == ("relay-user", "relay-pass")


def test_send_message_does_not_authenticate_when_no_username_is_given(stub_smtp):
    stub_smtp.advertise_auth = True

    send_message(_message(), host=stub_smtp.host, port=stub_smtp.port)

    assert stub_smtp.credentials is None
    assert not any(c.startswith("AUTH") for c in stub_smtp.commands)


def test_send_message_propagates_starttls_being_unsupported(stub_smtp):
    with pytest.raises(smtplib.SMTPException):
        send_message(
            _message(), host=stub_smtp.host, port=stub_smtp.port, starttls=True
        )

    assert stub_smtp.envelopes == []


def test_send_message_refuses_a_message_with_no_recipients_before_connecting(
    unreachable_smtp,
):
    host, port = unreachable_smtp
    message = _message()
    del message["To"]

    with pytest.raises(ValueError, match="recipient"):
        send_message(message, host=host, port=port)


# --- send_markdown_email ----------------------------------------------------


def test_send_markdown_email_builds_and_sends_in_one_call(stub_smtp):
    refused = send_markdown_email(
        MARKDOWN,
        subject="Weekly brief",
        sender=SENDER,
        recipients=RECIPIENTS,
        host=stub_smtp.host,
        port=stub_smtp.port,
        extra_headers={"X-Brief-Provider": "stub"},
        date=WHEN,
        msgid=MSGID,
    )

    assert refused == {}
    envelope = stub_smtp.last()
    assert envelope.rcpt_tos == RECIPIENTS
    assert "X-Brief-Provider: stub" in envelope.text
    delivered = {
        part.get_content_type(): part.get_content()
        for part in email.message_from_bytes(
            envelope.data, policy=policy.default
        ).walk()
        if not part.is_multipart()
    }
    # on the wire every line ends CRLF, as the protocol requires
    assert delivered["text/plain"].replace("\r\n", "\n") == MARKDOWN
    assert "<h1>Weekly brief</h1>" in delivered["text/html"]


def test_send_markdown_email_returns_refused_recipients(stub_smtp):
    stub_smtp.refuse["other@example.test"] = (550, b"Mailbox full")

    refused = send_markdown_email(
        MARKDOWN,
        subject="s",
        sender=SENDER,
        recipients=RECIPIENTS,
        host=stub_smtp.host,
        port=stub_smtp.port,
    )

    assert refused == {"other@example.test": (550, b"Mailbox full")}


def test_send_markdown_email_validates_before_opening_a_socket(unreachable_smtp):
    host, port = unreachable_smtp

    with pytest.raises(ValueError, match="recipient"):
        send_markdown_email(
            MARKDOWN, subject="s", sender=SENDER, recipients=[], host=host, port=port
        )
    with pytest.raises(ValueError, match="sender"):
        send_markdown_email(
            MARKDOWN,
            subject="s",
            sender="",
            recipients=RECIPIENTS,
            host=host,
            port=port,
        )
