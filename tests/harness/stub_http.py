"""A real stub HTTP server on an ephemeral port, for testing HTTP clients.

Copy this file into your feature's ``tests/harness/`` directory. It is vendored,
not imported from the library — features stay standalone by construction.

**Why not mock ``urllib``/``requests``?** Because then you test the mock. URL
construction, header handling, streaming response parsing, timeouts, and
connection failures are what actually break, and a mock asserts only that you
called it the way you thought you did. A real socket costs about a millisecond
per test and exercises the whole path.

Usage::

    from harness.stub_http import stub_http  # noqa: F401  (pytest fixture)

    def test_it(stub_http):
        stub_http.route("/api/thing", {"ok": True})
        client = MyClient(stub_http.url)
        assert client.get_thing() == {"ok": True}
        assert stub_http.requests[-1].path == "/api/thing"

Handlers may be a plain value (sent as JSON), bytes (sent verbatim), or a
callable taking the ``Request`` and returning either.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

import pytest


@dataclass
class Request:
    method: str
    path: str
    headers: dict = field(default_factory=dict)
    body: bytes = b""

    @property
    def json(self) -> Any:
        """Parsed JSON body, or None if there isn't one."""
        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except json.JSONDecodeError:
            return None


@dataclass
class Response:
    """Return one of these from a handler to control status or headers."""

    body: Any = b""
    status: int = 200
    headers: dict = field(default_factory=dict)


Handler = Callable[[Request], Any] | Any


class StubHTTP:
    """A running HTTP server whose routes tests can rewrite freely."""

    def __init__(self):
        self.routes: dict[tuple[str | None, str], Handler] = {}
        self.requests: list[Request] = []
        self.url = ""
        #: set True to make every route fail with 503
        self.down = False

    # ------------------------------------------------------------ setup ---

    def route(self, path: str, handler: Handler, method: str | None = None) -> None:
        """Register ``handler`` for ``path``. ``method=None`` matches any."""
        self.routes[(method, path)] = handler

    def routes_from(self, mapping: dict[str, Handler]) -> None:
        for path, handler in mapping.items():
            self.route(path, handler)

    # ------------------------------------------------------ inspection ---

    def requests_to(self, path: str) -> list[Request]:
        return [r for r in self.requests if r.path == path]

    def last(self, path: str | None = None) -> Request:
        pool = self.requests_to(path) if path else self.requests
        assert pool, f"no request recorded{f' for {path}' if path else ''}"
        return pool[-1]

    def reset(self) -> None:
        self.requests.clear()

    # -------------------------------------------------------- internals ---

    def _resolve(self, request: Request) -> Handler | None:
        for key in ((request.method, request.path), (None, request.path)):
            if key in self.routes:
                return self.routes[key]
        return None


def ndjson(*objects: Any) -> bytes:
    """Newline-delimited JSON, the shape streaming APIs use."""
    return ("\n".join(json.dumps(o) for o in objects) + "\n").encode()


class _RequestHandler(BaseHTTPRequestHandler):
    stub: StubHTTP

    def log_message(self, *args):  # keep pytest output clean
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        request = Request(
            method=self.command,
            path=self.path,
            headers=dict(self.headers),
            body=self.rfile.read(length) if length else b"",
        )
        self.stub.requests.append(request)

        if self.stub.down:
            self.send_error(503, "stub server is down")
            return

        handler = self.stub._resolve(request)
        if handler is None:
            self.send_error(404, f"no stub route for {request.path}")
            return

        result = handler(request) if callable(handler) else handler
        response = result if isinstance(result, Response) else Response(result)

        if isinstance(response.body, bytes):
            body = response.body
            content_type = "application/octet-stream"
        else:
            body = json.dumps(response.body).encode()
            content_type = "application/json"

        self.send_response(response.status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle


@pytest.fixture
def stub_http():
    """A running stub server. Yields the handle; shuts down on teardown."""
    stub = StubHTTP()
    handler = type("BoundHandler", (_RequestHandler,), {"stub": stub})
    server = HTTPServer(("127.0.0.1", 0), handler)
    stub.url = f"http://127.0.0.1:{server.server_port}"

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
def unreachable_url() -> str:
    """A URL with nothing listening, for connection-failure paths."""
    return "http://127.0.0.1:1"
