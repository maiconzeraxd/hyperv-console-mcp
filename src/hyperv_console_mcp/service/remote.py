"""The service across a boundary: one envelope, one codec, one dispatch table.

The MCP server and the dashboard each build their own ``HyperVService`` today,
so they own separate workers, separate serial and KD sessions, and separate
state — a session the AI opens is invisible to the window. The fix is one owner
behind a pipe. This module is everything about that boundary that does not need
a pipe to test: the request and response envelopes, the encoding of the value
types that are not JSON, and the table that decides which methods may cross.

A request names a public method and carries its arguments; a response carries
either the encoded result or the error's code, message and fix. Anything that
is not JSON — a screenshot's PNG, a bare byte string, a path — is encoded
explicitly rather than left to a serializer's accident, and a value the codec
cannot represent is a refusal, not a silent ``str()``.
"""

from __future__ import annotations

import base64
import inspect
from pathlib import Path
from typing import Any, Callable, Mapping

from ..capture import Screenshot
from ..errors import (
    BackendError,
    CaptureError,
    DebuggerError,
    HyperVError,
    PolicyError,
    ValidationError,
)

PROTOCOL_VERSION = 1

# The three errors whose code is fixed by their class. Everything else is a
# backend failure, which is what an unrecognised code means anyway.
_BY_CLASS: dict[str, type[HyperVError]] = {
    "invalid_argument": ValidationError,
    "not_authorized": PolicyError,
    "console_capture_failed": CaptureError,
}

Envelope = dict[str, Any]
Transport = Callable[[Envelope], Envelope]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: Any) -> bytes:
    return base64.b64decode(str(text or "").encode("ascii"))


def encode(value: Any) -> Any:
    """A JSON-safe copy of a service value, or a refusal.

    Tuples stay tuples through a round trip: a result that changes type across
    the boundary is a difference the caller would have to know about, and the
    whole point of this layer is that it has none.
    """
    if isinstance(value, Screenshot):
        return {
            "__type__": "screenshot",
            "png": _b64(value.png),
            "metadata": encode(value.metadata),
        }
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__type__": "bytes", "data": _b64(bytes(value))}
    if isinstance(value, Path):
        return {"__type__": "path", "path": str(value)}
    if isinstance(value, Mapping):
        return {str(key): encode(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return {"__type__": "tuple", "items": [encode(item) for item in value]}
    if isinstance(value, (list, set, frozenset)):
        return [encode(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"{type(value).__name__} cannot cross the service boundary.")


def decode(value: Any) -> Any:
    """The inverse of :func:`encode`."""
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get("__type__")
    if kind == "screenshot":
        return Screenshot(
            png=_unb64(value.get("png")), metadata=decode(value.get("metadata") or {})
        )
    if kind == "bytes":
        return _unb64(value.get("data"))
    if kind == "path":
        return Path(str(value.get("path") or ""))
    if kind == "tuple":
        return tuple(decode(item) for item in value.get("items") or [])
    return {key: decode(item) for key, item in value.items()}


def dispatch(service: Any, request: Mapping[str, Any]) -> Envelope:
    """Run one request against the service and return its envelope.

    Only public methods cross. A private name is refused rather than looked up,
    because ``_call`` and ``_approve`` would run an operation with the policy
    checks the caller was supposed to go through, and ``backend`` would hand the
    pipe out a second door to the host.
    """
    method = request.get("method")
    if not isinstance(method, str) or not method.isidentifier() or method.startswith("_"):
        return _error("invalid_argument", f"Unknown service method {method!r}.")
    function = getattr(service, method, None)
    if function is None or not callable(function):
        return _error("invalid_argument", f"Unknown service method {method!r}.")
    args = decode(list(request.get("args") or []))
    kwargs = decode(dict(request.get("kwargs") or {}))
    signature = _signature(function)
    if signature is not None:
        try:
            signature.bind(*args, **kwargs)
        except TypeError as exc:
            # A binding failure names the arguments, which is what a client that
            # called with the wrong shape needs to read.
            return _error("invalid_argument", f"{method}: {exc}")
    try:
        result = function(*args, **kwargs)
    except HyperVError as exc:
        return _error(exc.code, str(exc), exc.fix)
    except Exception as exc:  # noqa: BLE001 — the boundary never leaks a traceback
        return _error("internal_error", f"{method} failed: {type(exc).__name__}")
    try:
        return {"ok": encode(result)}
    except TypeError as exc:
        return _error("internal_error", f"{method} returned a value that cannot cross: {exc}")


def _signature(function: Any) -> Any:
    try:
        return inspect.signature(function)
    except (TypeError, ValueError):
        return None


def _error(code: str, message: str, fix: Mapping[str, Any] | None = None) -> Envelope:
    error: Envelope = {"code": code, "message": message}
    if fix:
        error["fix"] = dict(fix)
    return {"error": error}


def error_from(envelope: Mapping[str, Any]) -> HyperVError:
    """Rebuild the exception a response describes, keeping its code and fix."""
    code = str(envelope.get("code") or "backend_error")
    message = str(envelope.get("message") or "")
    fix = envelope.get("fix")
    kind = _BY_CLASS.get(code)
    if kind is not None:
        return kind(message, fix=fix)
    if code.startswith("kd_"):
        return DebuggerError(message, code=code, fix=fix)
    return BackendError(message, code=code, fix=fix)


class ServiceProxy:
    """The service's public shape, forwarded one call at a time.

    ``settings`` is deliberately local: both clients read the same environment
    and the same access file, and a policy that crossed the wire would be a
    policy a client could edit.
    """

    def __init__(self, transport: Transport, settings: Any = None) -> None:
        self._transport = transport
        self.settings = settings

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args: Any, **kwargs: Any) -> Any:
            response = self._transport({
                "method": name,
                "args": encode(list(args)),
                "kwargs": encode(dict(kwargs)),
            })
            if "error" in response:
                raise error_from(response["error"])
            return decode(response.get("ok"))

        call.__name__ = name
        return call


def local_transport(service: Any) -> Transport:
    """Dispatch inside this process — what the pipe replaces, not what it emulates."""
    return lambda request: dispatch(service, request)


__all__ = [
    "PROTOCOL_VERSION",
    "Envelope",
    "ServiceProxy",
    "Transport",
    "decode",
    "dispatch",
    "encode",
    "error_from",
    "local_transport",
]
