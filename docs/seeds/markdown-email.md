# Seed boundary: markdown-email

## Purpose
Deliver a Markdown document as a multipart/alternative email (styled HTML plus the raw Markdown as plain text) through an SMTP relay.

## when_to_use (draft for FEATURE.toml)
A scheduled job produced a Markdown document and the people who need it read email, not repositories — it has to land in a mail client looking like a page while the raw Markdown stays in the message for anyone who greps, quotes, or replies.

## Inputs
- markdown_text: str — the document; also becomes the text/plain part verbatim
- subject: str — already rendered by the caller (no template expansion here)
- sender: str — RFC 5322 address, e.g. 'brief@facility.iastate.edu'
- recipients: Sequence[str] — To: addresses
- host: str, port: int — SMTP relay; no default host, port defaults to 25
- stylesheet: str — CSS placed in a <style> block in <head>; DEFAULT_STYLESHEET if omitted
- extra_headers: Mapping[str, str] | None — e.g. Reply-To, List-Id, X-Brief-Provider
- timeout: float, starttls: bool, username/password: str | None — connection knobs, all explicit values

## Outputs
- build_message -> email.message.EmailMessage with text/plain (the Markdown, utf-8) and text/html alternatives, Subject/From/To/Date/Message-ID set
- markdown_to_html -> str, a complete HTML document with the stylesheet inlined in <head>
- send_message / send_markdown_email -> dict[str, tuple[int, bytes]] of refused recipients (empty on full success), as smtplib.send_message returns
- raises smtplib.SMTPException (or OSError on connect/timeout) unchanged; raises ValueError before any socket is opened when recipients is empty or sender is blank

## Dependencies
- **markdown** — The spec names Python `markdown` for the HTML conversion; hand-rolling a CommonMark subset (links, lists, headings, emphasis) is where a 'small' converter grows into hundreds of lines and silently mis-renders a citation link. Imported lazily inside markdown_to_html so build/send of a pre-rendered HTML body never needs it.
- **stdlib: email.message, email.utils, smtplib, html** — EmailMessage builds the multipart/alternative correctly (charset, transfer encoding, boundaries); smtplib is the only relay client needed; html.escape guards the <title>.

## Must NOT know about
- config.yaml or the `smtp:` block — host/port arrive as arguments
- the profile schema or its `delivery:` block — from/to/subject are already-resolved strings
- the `{title} — week of {week_start}` subject template — the caller renders it
- the Item dataclass, the briefs table, sent_at, or the --resend idempotency rule
- SQLite, the git archive, briefs/<profile>/ paths, or any filesystem path
- logging configuration — it returns/raises; the caller logs
- the footer text, provider name, or 'n of m candidates' line — those are already inside the Markdown
- environment variables or dotenv — credentials, if any, are passed in
- the stub-email fallback for an unavailable provider — that is a different Markdown body, same function

## Public API
```python
DEFAULT_STYLESHEET: str  # ~40 lines: body font/width, headings, links, lists, footer; no images
def markdown_to_html(markdown_text: str, *, stylesheet: str = DEFAULT_STYLESHEET, title: str = "") -> str
def build_message(markdown_text: str, *, subject: str, sender: str, recipients: Sequence[str], stylesheet: str = DEFAULT_STYLESHEET, extra_headers: Mapping[str, str] | None = None) -> EmailMessage
def send_message(message: EmailMessage, *, host: str, port: int = 25, timeout: float = 30.0, starttls: bool = False, username: str | None = None, password: str | None = None) -> dict[str, tuple[int, bytes]]
def send_markdown_email(markdown_text: str, *, subject: str, sender: str, recipients: Sequence[str], host: str, port: int = 25, stylesheet: str = DEFAULT_STYLESHEET, extra_headers: Mapping[str, str] | None = None, timeout: float = 30.0, starttls: bool = False, username: str | None = None, password: str | None = None) -> dict[str, tuple[int, bytes]]
```

## Test harness
none

## Approved module docstring (write this verbatim into the module)
```
Send a Markdown document as a multipart/alternative email through an SMTP relay.

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
```

## Prior art
verdict: seed

Nothing in the library sends email or converts Markdown to HTML. Four semantic searches plus an rg over every feature for smtplib/email.mime/EmailMessage returned no whole feature and no part; the only 'markdown' hits are features that emit Markdown text. The closest reusable thing is the stub_http harness pattern (threaded server on an ephemeral port), which the seed's tests should imitate for a stub SMTP server rather than mock smtplib.

## Critic verdict: keep
One job (email this Markdown); build vs send is the test seam, not a second seed. markdown dep is justified and lazy.

### Boundary fixes to apply at write time
- test_harness says 'none' but the docstring promises a stub SMTP socket server; set test_harness to stub_smtp and plan the ~40-line harness (smtpd was removed in Python 3.12; do not reach for aiosmtpd, write the minimal EHLO/MAIL/RCPT/DATA/QUIT responder)
- inputs example 'brief@facility.iastate.edu' — drop the domain from the seed's docs
- Date/Message-ID are set in build_message; make Message-ID domain derive from sender (email.utils.make_msgid(domain=...)) so no hostname leaks and goldens are stable with an injectable msgid: str | None
- stylesheet default is ~40 lines inside the module; fine, but keep it a constant a caller can replace wholesale, not appended to
