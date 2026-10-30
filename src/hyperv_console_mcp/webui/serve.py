"""The same page in the operator's own browser: loopback, one token, nothing remote.

The WebView2 window is the default because it opens no port at all. This mode
exists for the operator who wants the page in a browser tab, and it is the only
listener this project ever opens, so every lock it has is deliberate:

- **Loopback only.** The socket binds ``127.0.0.1``; ``0.0.0.0`` is never used,
  so there is nothing to reach from another machine. For that, use an SSH tunnel
  to the loopback.
- **One token, generated per run.** It is printed with the URL, required on
  every request, and never written to disk. The page receives it in a meta tag
  and sends it in a header, which is also what makes a cross-site POST useless:
  another site cannot read the token, and a plain form cannot set a header.
- **`Origin` and `Host` are checked.** The first stops a page on another site
  from driving this one; the second stops DNS rebinding, where a name the
  attacker controls resolves to 127.0.0.1 and the socket looks local while the
  name does not.
- **The calls are the window's own.** The listener speaks to the same
  :class:`~hyperv_console_mcp.webui.api.Api` the WebView2 window uses, which
  means the same service, the same policy, the same audit trail and the same
  refusals — this module adds a transport, not a door.

Long-polling carries the live feeds: ``events``, ``logs`` and ``kd_output`` are
already held-open service calls, so the page streams without a websocket and
this file needs no second protocol.
"""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..errors import HyperVError
from .api import Api

TOKEN_HEADER = "X-HyperV-Token"
# The names a loopback origin can legitimately carry. A host header that is not
# one of these is a name the attacker controls pointing at 127.0.0.1.
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_BODY_BYTES = 8 * 1024 * 1024
ASSETS = Path(__file__).with_name("assets")
EMPTY_META = 'name="hyperv-token" content=""'


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _page_with_token(page: str, token: str) -> str:
    """The same page, carrying the token the listener was opened with."""
    return page.replace(EMPTY_META, f'name="hyperv-token" content="{token}"')


def _handler(token: str, api: Api, page: str) -> type[BaseHTTPRequestHandler]:
    """One handler class per listener, so the token lives in no global."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "hyperv-console"
        sys_version = ""

        # -- plumbing -----------------------------------------------------

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            """No access log on stderr: the unified log stream is the record."""

        def _send(self, status: int, body: dict[str, Any] | str) -> None:
            payload = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            data = payload.encode("utf-8")
            self.send_response(status)
            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8" if isinstance(body, str) else "application/json",
            )
            self.send_header("Content-Length", str(len(data)))
            # The page is one file with everything inline; nothing may be cached
            # by a browser that could then be reopened without a token.
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def _token_ok(self, supplied: str | None) -> bool:
            return bool(supplied) and secrets.compare_digest(supplied, token)

        def _host_ok(self) -> bool:
            name, _, port = self.headers.get("Host", "").partition(":")
            if name.casefold() not in LOOPBACK_NAMES:
                return False
            return not port or port == str(self.server.server_address[1])

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                # A same-origin navigation or a tool like curl. The token is the
                # guard on a call; an absent Origin is not a foreign one.
                return True
            parsed = urlparse(origin)
            return (
                parsed.scheme == "http"
                and (parsed.hostname or "").casefold() in LOOPBACK_NAMES
                and parsed.port == self.server.server_address[1]
            )

        # -- routes -------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - the base class names it
            parsed = urlparse(self.path)
            if parsed.path not in {"/", "/index.html"}:
                self._send(HTTPStatus.NOT_FOUND, _error("not_found", "No such path."))
                return
            supplied = parse_qs(parsed.query).get("token", [None])[0] or self.headers.get(
                TOKEN_HEADER
            )
            if not self._host_ok():
                self._send(HTTPStatus.FORBIDDEN, _error("forbidden_host", "This listener answers only to a loopback name."))
                return
            if not self._token_ok(supplied):
                self._send(
                    HTTPStatus.UNAUTHORIZED,
                    _error("not_authorized", "Open the URL this process printed, which carries its token."),
                )
                return
            self._send(HTTPStatus.OK, _page_with_token(page, token))

        def do_POST(self) -> None:  # noqa: N802 - the base class names it
            if urlparse(self.path).path != "/api":
                self._send(HTTPStatus.NOT_FOUND, _error("not_found", "No such path."))
                return
            if not self._token_ok(self.headers.get(TOKEN_HEADER)):
                self._send(
                    HTTPStatus.UNAUTHORIZED,
                    _error("not_authorized", "Every call needs the token this listener was started with."),
                )
                return
            if not self._host_ok():
                self._send(HTTPStatus.FORBIDDEN, _error("forbidden_host", "This listener answers only to a loopback name."))
                return
            if not self._origin_ok():
                self._send(HTTPStatus.FORBIDDEN, _error("forbidden_origin", "A page from another origin may not call this listener."))
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send(HTTPStatus.BAD_REQUEST, _error("invalid_argument", "A call must carry a JSON body of a sensible size."))
                return
            try:
                request = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send(HTTPStatus.BAD_REQUEST, _error("invalid_argument", "The body is not JSON."))
                return
            self._send(HTTPStatus.OK, self._call(request))

        def _call(self, request: Any) -> dict[str, Any]:
            if not isinstance(request, dict):
                return _error("invalid_argument", "A call is an object with method and arguments.")
            method = request.get("method")
            arguments = request.get("arguments") or []
            if not isinstance(method, str) or not isinstance(arguments, list):
                return _error("invalid_argument", "A call needs a method name and a list of arguments.")
            # Only the public surface crosses: the window's own private helpers
            # are not a door, and neither is anything Python happens to have.
            if method.startswith("_") or not callable(getattr(api, method, None)):
                return _error(
                    "invalid_argument",
                    f"{method} is not a method of this window.",
                )
            try:
                value = getattr(api, method)(*arguments)
            except HyperVError as exc:
                return _error(exc.code, str(exc))
            except TypeError as exc:
                return _error("invalid_argument", f"{method} was not called with the arguments it takes: {exc}")
            except Exception as exc:  # noqa: BLE001 - the page draws answers, not tracebacks
                return _error("internal_error", f"{method} failed: {type(exc).__name__}")
            # The window's methods already answer in the envelope the page
            # knows — `{ok: ...}` or `{error: ...}` — so it crosses unchanged
            # and the served page sees exactly what the bridge would give it.
            return value if isinstance(value, dict) else {"ok": value}

    return Handler


class ServeHandle:
    """A listener in a thread, with the URL and the token it was opened with."""

    def __init__(self, server: ThreadingHTTPServer, token: str, thread: threading.Thread) -> None:
        self._server = server
        self.token = token
        self._thread = thread

    @property
    def host(self) -> str:
        return str(self._server.server_address[0])

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self.token}"

    def wait(self) -> None:
        """Serve in the calling thread, which is what the command does."""
        self._server.serve_forever()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def start(
    api: Api,
    *,
    port: int = 0,
    open_browser: bool = False,
    page: Path | None = None,
) -> ServeHandle:
    """Bind 127.0.0.1, serve the page, and answer the page's calls."""
    text = (page or (ASSETS / "index.html")).read_text(encoding="utf-8")
    token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(("127.0.0.1", int(port)), _handler(token, api, text))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="hvmcp-serve")
    thread.start()
    handle = ServeHandle(server, token, thread)
    if open_browser:
        webbrowser.open(handle.url)
    return handle


def run(*, port: int = 0, open_browser: bool = True, vm_name: str = "") -> int:
    """The ``--serve`` command: the shared service, the page, one URL."""
    from ..config import Settings
    from ..service.host import open_service

    settings = Settings.from_env()
    service, backend = open_service(settings, actor="ui")
    try:
        handle = start(Api(service, settings, vm_name), port=port, open_browser=open_browser)
        print(f"hyperv-console: {handle.url}", flush=True)
        print("Press Ctrl+C to stop the page. The service keeps running.", flush=True)
        try:
            handle.wait()
        except KeyboardInterrupt:
            pass
        finally:
            handle.close()
    finally:
        if backend is not None:
            backend.close()
    return 0


__all__ = ["MAX_BODY_BYTES", "ServeHandle", "TOKEN_HEADER", "run", "start"]
