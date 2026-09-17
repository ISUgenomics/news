"""A real stub SMTP server on an ephemeral port, for testing mail senders.

Copy this file into your feature's ``tests/harness/`` directory. It is vendored,
not imported from the library — features stay standalone by construction.

**Why not mock ``smtplib``?** Because then you test the mock. Envelope
construction (MAIL FROM / RCPT TO), dot-stuffing in DATA, header folding,
transfer encoding, per-recipient refusals, and AUTH are what actually break, and
a mock asserts only that you called it the way you thought you did. A real
socket costs about a millisecond per test and exercises the whole path.

The stdlib ``smtpd`` module was removed in Python 3.12, so this is a minimal
EHLO/MAIL/RCPT/DATA/QUIT responder written on ``socketserver`` — no third-party
mail server.

Usage::

    from harness.stub_smtp import stub_smtp  # noqa: F401  (pytest fixture)

    def test_it(stub_smtp):
        send_it(host=stub_smtp.host, port=stub_smtp.port)
        envelope = stub_smtp.last()
        assert envelope.mail_from == "brief@example.test"
        assert envelope.rcpt_tos == ["reader@example.test"]
        assert b"Subject: Weekly brief" in envelope.data

Knobs:

``stub.refuse[address] = (code, b"text")``
    make RCPT TO for that address fail with that reply.
``stub.advertise_auth = True``
    advertise ``AUTH PLAIN LOGIN``; credentials land in ``stub.credentials``.
``stub.down = True``
    greet every new connection with ``421`` and hang up.
"""

from __future__ import annotations

import base64
import email
import socketserver
import threading
from dataclasses import dataclass, field
from email.message import Message

import pytest


@dataclass
class Envelope:
    """One accepted message: the envelope the relay saw, and the raw DATA."""

    mail_from: str
    rcpt_tos: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    data: bytes = b""

    @property
    def text(self) -> str:
        """The raw message decoded as utf-8, for substring assertions."""
        return self.data.decode("utf-8", "replace")

    @property
    def message(self) -> Message:
        """The raw DATA parsed back into an ``email.message.Message``."""
        return email.message_from_bytes(self.data)


class StubSMTP:
    """A running SMTP server whose replies tests can rewrite freely."""

    def __init__(self):
        self.host = "127.0.0.1"
        self.port = 0
        self.envelopes: list[Envelope] = []
        #: commands received, verbatim, across all connections
        self.commands: list[str] = []
        #: address -> (code, text) to refuse at RCPT TO
        self.refuse: dict[str, tuple[int, bytes]] = {}
        #: set True to advertise AUTH PLAIN LOGIN
        self.advertise_auth = False
        #: (username, password) from the last successful AUTH
        self.credentials: tuple[str, str] | None = None
        #: set True to refuse every new connection with 421
        self.down = False

    # ------------------------------------------------------ inspection ---

    def last(self) -> Envelope:
        assert self.envelopes, "no message was delivered to the stub SMTP server"
        return self.envelopes[-1]

    def reset(self) -> None:
        self.envelopes.clear()
        self.commands.clear()
        self.credentials = None

    # -------------------------------------------------------- internals ---

    def _rcpt_reply(self, address: str) -> tuple[int, bytes]:
        return self.refuse.get(address, (250, b"OK"))


def _address(argument: str) -> str:
    """The address out of ``FROM:<a@b> BODY=8BITMIME`` or ``TO:<a@b>``."""
    _, _, rest = argument.partition(":")
    rest = rest.strip()
    if rest.startswith("<"):
        return rest[1 : rest.index(">")] if ">" in rest else rest[1:]
    return rest.split()[0] if rest.split() else ""


class _Handler(socketserver.StreamRequestHandler):
    stub: StubSMTP

    def _say(self, line: bytes) -> None:
        self.wfile.write(line + b"\r\n")
        self.wfile.flush()

    def handle(self):  # noqa: C901 - a protocol dispatch, flat on purpose
        if self.stub.down:
            self._say(b"421 stub server is down")
            return
        self._say(b"220 stub.invalid ESMTP stub_smtp")
        mail_from: str | None = None
        rcpt_tos: list[str] = []
        refused: list[str] = []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            self.stub.commands.append(line)
            verb, _, argument = line.partition(" ")
            verb = verb.upper()

            if verb == "EHLO":
                replies = [
                    b"250-stub.invalid Hello",
                    b"250-SIZE 33554432",
                    b"250-8BITMIME",
                ]
                if self.stub.advertise_auth:
                    replies.append(b"250-AUTH PLAIN LOGIN")
                replies.append(b"250 HELP")
                for reply in replies:
                    self._say(reply)
            elif verb == "HELO":
                self._say(b"250 stub.invalid Hello")
            elif verb == "AUTH":
                self._auth(argument)
            elif verb == "MAIL":
                mail_from = _address(argument)
                rcpt_tos, refused = [], []
                self._say(b"250 OK sender accepted")
            elif verb == "RCPT":
                address = _address(argument)
                code, text = self.stub._rcpt_reply(address)
                if code < 400:
                    rcpt_tos.append(address)
                else:
                    refused.append(address)
                self._say(f"{code} ".encode() + text)
            elif verb == "DATA":
                if not rcpt_tos:
                    self._say(b"503 no valid recipients")
                    continue
                self._say(b"354 End data with <CR><LF>.<CR><LF>")
                data = self._read_data()
                self.stub.envelopes.append(
                    Envelope(
                        mail_from=mail_from or "",
                        rcpt_tos=list(rcpt_tos),
                        refused=list(refused),
                        data=data,
                    )
                )
                mail_from, rcpt_tos, refused = None, [], []
                self._say(b"250 OK message queued")
            elif verb == "RSET":
                mail_from, rcpt_tos, refused = None, [], []
                self._say(b"250 OK")
            elif verb == "NOOP":
                self._say(b"250 OK")
            elif verb == "QUIT":
                self._say(b"221 Bye")
                return
            else:
                self._say(b"502 Command not implemented")

    def _auth(self, argument: str) -> None:
        mechanism, _, initial = argument.partition(" ")
        mechanism = mechanism.upper()
        if mechanism == "PLAIN":
            blob = initial.strip()
            if not blob:
                self._say(b"334 ")
                blob = self.rfile.readline().decode().strip()
            parts = base64.b64decode(blob).split(b"\0")
            username, password = (parts + [b"", b"", b""])[1:3]
            self.stub.credentials = (username.decode(), password.decode())
            self._say(b"235 Authentication succeeded")
        elif mechanism == "LOGIN":
            self._say(b"334 " + base64.b64encode(b"Username:"))
            username = base64.b64decode(self.rfile.readline().strip()).decode()
            self._say(b"334 " + base64.b64encode(b"Password:"))
            password = base64.b64decode(self.rfile.readline().strip()).decode()
            self.stub.credentials = (username, password)
            self._say(b"235 Authentication succeeded")
        else:
            self._say(b"504 Unrecognized authentication type")

    def _read_data(self) -> bytes:
        lines: list[bytes] = []
        while True:
            raw = self.rfile.readline()
            if not raw or raw in (b".\r\n", b".\n"):
                break
            line = raw.rstrip(b"\r\n")
            if line.startswith(b".."):  # undo SMTP dot-stuffing
                line = line[1:]
            lines.append(line)
        return b"\r\n".join(lines)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture
def stub_smtp():
    """A running stub SMTP server. Yields the handle; shuts down on teardown."""
    stub = StubSMTP()
    handler = type("BoundHandler", (_Handler,), {"stub": stub})
    server = _Server(("127.0.0.1", 0), handler)
    stub.port = server.server_address[1]

    # shutdown() blocks for one poll interval; the 0.5s default would add half
    # a second to EVERY test's teardown
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def unreachable_smtp() -> tuple[str, int]:
    """A host/port with nothing listening, for connection-failure paths."""
    return ("127.0.0.1", 1)
