"""The page in the operator's own browser: loopback, one token, no remote.

`--serve` is the second delivery of the same HTML file. It exists for the
operator who wants the window in a browser tab, and it is the only mode in this
project that opens a port at all — so the locks are the test: loopback only, a
token on every request, `Origin` and `Host` checked against DNS rebinding, and
the same `Api` the WebView2 window uses, which is the same service the MCP
server talks to.
"""

from __future__ import annotations

import http.client
import json
from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.webui import serve
from hyperv_console_mcp.webui.api import Api


class FakeService:
    """Just enough service for the envelope, with one private name to refuse."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_vms(self) -> dict[str, Any]:
        self.calls.append("list_vms")
        return {"vms": [{"name": "NORTH-Test"}]}

    def _store(self) -> dict[str, Any]:  # pragma: no cover - must never be reached
        raise AssertionError("a private name crossed the listener")


@pytest.fixture
def listener():
    service = FakeService()
    handle = serve.start(Api(service, Settings()), port=0, open_browser=False)
    try:
        yield handle, service
    finally:
        handle.close()


def _request(
    handle: serve.ServeHandle,
    method: str = "POST",
    path: str = "/api",
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any] | str]:
    connection = http.client.HTTPConnection("127.0.0.1", handle.port, timeout=5)
    try:
        payload = None if body is None else json.dumps(body)
        connection.request(method, path, body=payload, headers=headers or {})
        response = connection.getresponse()
        text = response.read().decode("utf-8")
        try:
            return response.status, json.loads(text)
        except ValueError:
            return response.status, text
    finally:
        connection.close()


def _token_headers(handle: serve.ServeHandle, **extra: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json", serve.TOKEN_HEADER: handle.token}
    headers.update(extra)
    return headers


def test_the_listener_is_bound_to_loopback_only(listener) -> None:
    handle, _ = listener

    assert handle.host == "127.0.0.1"
    assert handle.port > 0
    assert handle.url.startswith("http://127.0.0.1:")
    # 0.0.0.0 would be reachable from the network, which this mode never is.
    assert "0.0.0.0" not in handle.url


def test_a_request_without_the_token_is_refused(listener) -> None:
    handle, service = listener

    status, body = _request(
        handle, body={"method": "list_vms", "arguments": []},
        headers={"Content-Type": "application/json"},
    )

    assert status == 401
    assert body["error"]["code"] == "not_authorized"
    assert service.calls == []


def test_a_wrong_token_is_refused(listener) -> None:
    handle, service = listener

    status, _ = _request(
        handle, body={"method": "list_vms", "arguments": []},
        headers={"Content-Type": "application/json", serve.TOKEN_HEADER: "guess"},
    )

    assert status == 401
    assert service.calls == []


def test_a_foreign_origin_is_refused(listener) -> None:
    """A page on another site can send a POST; it cannot read the token."""
    handle, service = listener

    status, body = _request(
        handle, body={"method": "list_vms", "arguments": []},
        headers=_token_headers(handle, Origin="http://evil.example"),
    )

    assert status == 403
    assert body["error"]["code"] == "forbidden_origin"
    assert service.calls == []


def test_a_host_that_is_not_loopback_is_refused(listener) -> None:
    """DNS rebinding: the socket is loopback, but the name is not."""
    handle, service = listener

    status, body = _request(
        handle, body={"method": "list_vms", "arguments": []},
        headers=_token_headers(handle, Host=f"attacker.example:{handle.port}"),
    )

    assert status == 403
    assert body["error"]["code"] == "forbidden_host"
    assert service.calls == []


def test_a_call_with_the_token_reaches_the_service(listener) -> None:
    """The listener speaks the page's surface, which is `Api`, not the service."""
    handle, service = listener

    status, body = _request(
        handle, body={"method": "vms", "arguments": []}, headers=_token_headers(handle)
    )

    assert status == 200
    # The same envelope the bridge hands the page: `{ok: ...}`.
    assert body["ok"]["vms"] == [{"name": "NORTH-Test"}]
    assert service.calls == ["list_vms"]


def test_a_private_name_never_crosses_the_listener(listener) -> None:
    handle, _ = listener

    status, body = _request(
        handle, body={"method": "_store", "arguments": []}, headers=_token_headers(handle)
    )

    assert status == 200
    assert body["error"]["code"] == "invalid_argument"
    assert "_store" in body["error"]["message"]


def test_an_unknown_method_is_an_answer_not_a_traceback(listener) -> None:
    handle, _ = listener

    status, body = _request(
        handle, body={"method": "no_such_thing", "arguments": []}, headers=_token_headers(handle)
    )

    assert status == 200
    assert body["error"]["code"] == "invalid_argument"


def test_the_page_carries_the_token_it_was_opened_with(listener) -> None:
    handle, _ = listener

    status, text = _request(handle, method="GET", path=f"/?token={handle.token}")

    assert status == 200
    assert 'name="hyperv-token"' in text
    assert handle.token in text
    # The same page the WebView2 window loads, not a second copy of it.
    assert "pywebviewready" in text


def test_the_page_is_not_served_without_the_token(listener) -> None:
    handle, _ = listener

    status, _ = _request(handle, method="GET", path="/")

    assert status == 401


def test_the_token_is_not_a_fixed_string() -> None:
    """Two runs must not share a token, or one leaked tab opens the next host."""
    first = serve.start(Api(FakeService(), Settings()), port=0, open_browser=False)
    second = serve.start(Api(FakeService(), Settings()), port=0, open_browser=False)
    try:
        assert first.token != second.token
        assert len(first.token) >= 32
    finally:
        first.close()
        second.close()


def test_the_listener_stops_when_it_is_closed() -> None:
    handle = serve.start(Api(FakeService(), Settings()), port=0, open_browser=False)
    port = handle.port
    handle.close()

    with pytest.raises(OSError):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/")
            connection.getresponse()
        finally:
            connection.close()

def test_a_closed_tab_is_not_an_error(listener) -> None:
    """The dock long-polls, so a closed tab aborts a request mid-answer.

    That arrives as a connection reset. Printing a traceback for it would fill
    the service log with the normal end of a browser request.
    """
    handle, _ = listener

    try:
        raise ConnectionAbortedError(10053, "aborted by the host")
    except ConnectionAbortedError:
        # Returns quietly: nothing is written to stderr.
        handle._server.handle_error(None, ("127.0.0.1", 0))
