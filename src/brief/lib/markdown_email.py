"""Send a Markdown document as a multipart/alternative email through an SMTP relay.

The Markdown is the canonical form: it is sent verbatim as the text/plain part, and a
rendered HTML document with a small inline stylesheet is the text/html alternative, so
a mail client shows a page and a grep, a reply, or a text-only reader still gets the
source. Build and send are separate steps: `build_message` is pure and returns an
`EmailMessage` you can inspect or serialize; `send_message` opens the socket. The
convenience `send_markdown_email` does both.

Contract: every value is explicit (addresses, subject, host, port, stylesheet,
headers) — no config object, no environment lookup, no default relay host. Subject
and body arrive already rendered; this module does no templating. Recipients that the
relay refuses come back as the dict smtplib returns; transport errors propagate
unchanged so the caller decides whether to retry or record a failed send. An empty
recipient list or blank sender raises ValueError before any connection is attempted.

Deliberately not done: no attachments, no inline images, no tracking pixels, no
HTML sanitization (the Markdown is text the caller already trusts), no retry or
queueing, no idempotency (the caller owns "already sent" state), no address-book
or template expansion.

Dependencies: `markdown` (PyPI) for Markdown -> HTML, imported lazily inside
`markdown_to_html`; a hand-rolled converter is where a small module grows past its
worth and mis-renders the links that make the document trustworthy. Everything
else is stdlib (`email.message.EmailMessage`, `email.utils`, `smtplib`, `html`).

Tests: `build_message` is covered without a network; `send_message` runs against a
~40-line threaded stub SMTP socket server on an ephemeral port (tests/harness/stub_smtp.py,
modelled on the library's stub_http harness) that records the DATA it receives.

Details of the contract above, decided when this module was written:

- `build_message` takes `date: datetime | None` and `msgid: str | None`. Both default to
  None, which reads the clock and generates a random message id — pass them to get a
  byte-identical message, which is what makes goldens possible. The generated
  Message-ID's domain comes from the sender's address, never from the machine's
  hostname; a sender with no domain falls back to `localhost`. When `msgid` is given
  the MIME boundary is derived from it too, since a random boundary would defeat the
  point.
- Both parts are transfer-encoded quoted-printable, so the serialized message is 7-bit
  clean for a relay that does not advertise 8BITMIME. "Verbatim" is about content, not
  bytes on the wire: the text/plain part decodes back to the Markdown given, with the
  two normalizations `EmailMessage` applies to every text part — line endings become
  LF (a CRLF or CR source line arrives as LF) and a trailing newline is added when the
  text does not end in one, so an empty document becomes a single newline. Nothing
  else is added, stripped, re-wrapped, or re-ordered. Serialized for the wire the
  part's lines end CRLF, as the protocol requires; a mail client normalizes them.
- The HTML alternative is rendered by `markdown_to_html` from the same Markdown, with
  the `stylesheet` passed to `build_message` and with `subject` as its `<title>`. That
  is the only thing a header contributes to the body.
- Validation is the same in `build_message` and `send_markdown_email`, and runs before
  anything else: a blank sender, an empty recipient list, or a blank entry in it raises
  ValueError. `send_message` re-checks the message it is handed (To/Cc/Bcc all empty is
  a ValueError) so no socket is opened for a message that cannot be delivered.
- A carriage return or linefeed in `sender`, in a recipient, in `subject`, or in an
  `extra_headers` value raises ValueError. Those values become headers, and a line
  break in a header is header injection (a smuggled `Bcc:`), not formatting. It is
  checked in `build_message`, so `send_markdown_email` refuses before opening a socket.
- `extra_headers` are applied last and replace an identically-named header rather than
  adding a second one, so a caller can override Subject or Date deliberately.
- The stylesheet is replaced wholesale, never appended to: what a caller passes is the
  entire content of the single `<style>` block. `title=""` (the default) emits no
  `<title>` element; a title is HTML-escaped.
- Markdown is converted with the bundled `extra` extension, so tables, fenced code, and
  definition lists render; nothing else is enabled.
- A relay that refuses *some* recipients returns them as `{address: (code, message)}`;
  a relay that refuses *every* recipient makes smtplib raise `SMTPRecipientsRefused`,
  which propagates unchanged like any other transport error.
"""

from __future__ import annotations

import hashlib
import html as _html
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, getaddresses, localtime, make_msgid, parseaddr
from typing import Mapping, Sequence

__all__ = [
    "DEFAULT_STYLESHEET",
    "markdown_to_html",
    "build_message",
    "send_message",
    "send_markdown_email",
]


#: The whole content of the single ``<style>`` block. A caller replaces it wholesale;
#: nothing is ever appended to it. No images, no web fonts, no media queries a mail
#: client would strip anyway.
DEFAULT_STYLESHEET = """\
body {
  font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 15px;
  line-height: 1.55;
  color: #1a1a1a;
  background: #ffffff;
  max-width: 42em;
  margin: 0 auto;
  padding: 1.5em 1.25em 3em;
}
h1, h2, h3 {
  line-height: 1.25;
  color: #111111;
  margin: 1.6em 0 0.5em;
}
h1 { font-size: 1.5em; border-bottom: 1px solid #e2e2e2; padding-bottom: 0.3em; }
h2 { font-size: 1.2em; }
h3 { font-size: 1.05em; }
p { margin: 0.7em 0; }
ul, ol { margin: 0.7em 0; padding-left: 1.4em; }
li { margin: 0.35em 0; }
a { color: #0b5fa5; text-decoration: underline; }
blockquote {
  margin: 0.9em 0;
  padding: 0.1em 1em;
  border-left: 3px solid #d8d8d8;
  color: #444444;
}
code, pre {
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.92em;
  background: #f5f5f5;
}
pre { padding: 0.8em; overflow-x: auto; }
table { border-collapse: collapse; margin: 0.9em 0; }
th, td { border: 1px solid #dddddd; padding: 0.35em 0.6em; text-align: left; }
hr { border: 0; border-top: 1px solid #e2e2e2; margin: 2em 0; }
hr + p, .footer { font-size: 0.86em; color: #666666; }
"""

_MARKDOWN_EXTENSIONS = ("extra",)


def markdown_to_html(
    markdown_text: str, *, stylesheet: str = DEFAULT_STYLESHEET, title: str = ""
) -> str:
    """Render ``markdown_text`` as a complete HTML document with ``stylesheet`` inlined.

    ``stylesheet`` becomes the entire content of the one ``<style>`` block. ``title`` is
    HTML-escaped and omitted entirely when empty. The ``markdown`` package is imported
    here, not at module import, so building or sending a message never needs it.
    """
    import markdown as _markdown  # lazy: see the module docstring

    rendered = _markdown.markdown(markdown_text, extensions=list(_MARKDOWN_EXTENSIONS))
    title_element = f"<title>{_html.escape(title)}</title>\n" if title else ""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{title_element}"
        "<style>\n"
        f"{stylesheet}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        f"{rendered}\n"
        "</body>\n"
        "</html>\n"
    )


def _check_header_value(label: str, value: str) -> str:
    """Refuse a line break in a value that becomes a header: that is header injection."""
    if "\r" in value or "\n" in value:
        raise ValueError(f"{label} must not contain a carriage return or linefeed")
    return value


def _check_addresses(sender: str, recipients: Sequence[str]) -> list[str]:
    """Validate the envelope. Raises ValueError before anything else happens."""
    if not sender or not sender.strip():
        raise ValueError("sender must be a non-empty address")
    _check_header_value("sender", sender)
    addresses = list(recipients)
    if not addresses:
        raise ValueError("recipients must name at least one address")
    for address in addresses:
        if not address or not address.strip():
            raise ValueError("recipients must not contain a blank address")
        _check_header_value("a recipient address", address)
    return addresses


def _sender_domain(sender: str) -> str:
    """The domain of ``sender``, or ``localhost`` — never the machine's hostname."""
    _, address = parseaddr(sender)
    _, at, domain = address.rpartition("@")
    return domain.strip() if at and domain.strip() else "localhost"


def build_message(
    markdown_text: str,
    *,
    subject: str,
    sender: str,
    recipients: Sequence[str],
    stylesheet: str = DEFAULT_STYLESHEET,
    extra_headers: Mapping[str, str] | None = None,
    date: datetime | None = None,
    msgid: str | None = None,
) -> EmailMessage:
    """Build the multipart/alternative message. Pure: opens no socket.

    The Markdown is the text/plain part verbatim (see the module docstring for the two
    line-ending normalizations that "verbatim" allows); its rendering by
    ``markdown_to_html``, with ``stylesheet`` and with ``subject`` as the ``<title>``, is
    the alternative. Pass ``date`` and ``msgid`` for a byte-identical message; omitted,
    the clock is read and a Message-ID is generated with the sender's domain.
    ``extra_headers`` are applied last and replace an identically-named header. A line
    break in any value that becomes a header raises ValueError.
    """
    addresses = _check_addresses(sender, recipients)

    _check_header_value("subject", subject)
    for name, value in (extra_headers or {}).items():
        _check_header_value(f"the {name} header", value)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(addresses)
    message["Date"] = format_datetime(date if date is not None else localtime())
    message["Message-ID"] = msgid if msgid else make_msgid(domain=_sender_domain(sender))

    message.set_content(
        markdown_text, subtype="plain", charset="utf-8", cte="quoted-printable"
    )
    message.add_alternative(
        markdown_to_html(markdown_text, stylesheet=stylesheet, title=subject),
        subtype="html",
        charset="utf-8",
        cte="quoted-printable",
    )
    if msgid:
        # A random MIME boundary would defeat the point of an injectable msgid.
        message.set_boundary("===" + hashlib.sha256(msgid.encode("utf-8")).hexdigest()[:24] + "===")

    for name, value in (extra_headers or {}).items():
        del message[name]
        message[name] = value
    return message


def send_message(
    message: EmailMessage,
    *,
    host: str,
    port: int = 25,
    timeout: float = 30.0,
    starttls: bool = False,
    username: str | None = None,
    password: str | None = None,
) -> dict[str, tuple[int, bytes]]:
    """Send ``message`` through the relay. Returns refused recipients, empty on success.

    Transport failures propagate unchanged: ``OSError`` on connect or timeout,
    ``smtplib.SMTPException`` (including ``SMTPRecipientsRefused`` when *every*
    recipient is refused) from the relay. A message with no To/Cc/Bcc raises ValueError
    before the socket is opened.
    """
    header_values = [
        value
        for name in ("To", "Cc", "Bcc")
        for value in message.get_all(name, [])
    ]
    if not [a for _, a in getaddresses([str(v) for v in header_values]) if a.strip()]:
        raise ValueError("message names no recipient in To, Cc, or Bcc")

    with smtplib.SMTP(host, port, timeout=timeout) as relay:
        relay.ehlo_or_helo_if_needed()
        if starttls:
            relay.starttls()
            relay.ehlo()
        if username is not None:
            relay.login(username, password or "")
        return relay.send_message(message)


def send_markdown_email(
    markdown_text: str,
    *,
    subject: str,
    sender: str,
    recipients: Sequence[str],
    host: str,
    port: int = 25,
    stylesheet: str = DEFAULT_STYLESHEET,
    extra_headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    starttls: bool = False,
    username: str | None = None,
    password: str | None = None,
    date: datetime | None = None,
    msgid: str | None = None,
) -> dict[str, tuple[int, bytes]]:
    """Build and send in one call. Returns refused recipients, empty on success."""
    message = build_message(
        markdown_text,
        subject=subject,
        sender=sender,
        recipients=recipients,
        stylesheet=stylesheet,
        extra_headers=extra_headers,
        date=date,
        msgid=msgid,
    )
    return send_message(
        message,
        host=host,
        port=port,
        timeout=timeout,
        starttls=starttls,
        username=username,
        password=password,
    )
