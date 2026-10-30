"""One service, one process, one pipe: the host and the client that reaches it.

The MCP server and the dashboard each build a ``HyperVService``, so they own
separate workers, separate serial and KD sessions, and separate state. This
module puts the service in a single background process and lets both of them
be clients of it.

The pipe is the approval broker's pipe with the same hardening, because it
carries the same kind of authority: a security descriptor granting only the
current user's SID, ``FILE_FLAG_FIRST_PIPE_INSTANCE`` on the first instance so
a second host fails instead of quietly sharing the name, ``PIPE_REJECT_REMOTE_CLIENTS``,
and a client that verifies the server is a Python process of the same user
before it sends anything.

Messages are framed with a length prefix rather than relying on message-mode
boundaries, because a screenshot is megabytes and a message-mode read that
outgrows its buffer is a bug waiting for the first large result.
"""

from __future__ import annotations

import ctypes
import json
import os
import struct
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Mapping

from .. import ipc
from ..errors import PolicyError
from .remote import Envelope, Transport, dispatch

SERVICE_PIPE_PREFIX = r"\\.\pipe\HyperVConsoleMCP-Service-"
HEADER_BYTES = 4
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
CONNECT_TIMEOUT_MS = 2_000
# One operation at a time, which is what the service already assumes: its
# managers are not built for two callers interleaving a checkpoint with a
# restore, and the audit trail reads better with one line per operation.
SERVICE_LOCK = threading.Lock()
# The methods that answer from the event bus alone. They are dispatched without
# the lock above: `events_wait` blocks by design, and a live feed that stops
# every other client for its duration is worse than no live feed.
LOCK_FREE = frozenset({"event_snapshot", "events_wait"})


def dispatch_request(
    service: Any, request: Mapping[str, Any], lock: threading.Lock
) -> dict[str, Any]:
    """Dispatch one request, taking the worker lock unless the method is bus-only.

    The lock serializes the backend worker, which is a single persistent
    PowerShell process. A method that only reads the event bus touches no
    worker, and `events_wait` holds its caller for up to 30 seconds by design —
    taking the lock for that would stop every other client for the same time.
    """
    if request.get("method") in LOCK_FREE:
        return dispatch(service, request)
    with lock:
        return dispatch(service, request)


def pipe_name(suffix: str = "") -> str:
    """The service pipe for this user, so two users on one host never collide.

    ``HYPERV_MCP_SERVICE_PIPE`` names another pipe on purpose, which is what a
    second session that must not share state with the first uses; the suffix is
    the test-only way to get a pipe nothing else can be listening on.
    """
    if not suffix:
        override = os.environ.get("HYPERV_MCP_SERVICE_PIPE", "").strip()
        if override:
            # CreateNamedPipeW answers a malformed name with WinError 123 from
            # the detached host, where nobody reads it; say it here instead.
            if not override.lower().startswith("\\\\.\\pipe\\") or len(override) <= 9:
                raise PolicyError(
                    "HYPERV_MCP_SERVICE_PIPE must name a pipe, for example "
                    r"\\.\pipe\HyperVConsoleMCP-Service-mine."
                )
            return override
    return f"{SERVICE_PIPE_PREFIX}{ipc._current_user_sid()}{suffix}"


def _send(handle: Any, message: Mapping[str, Any]) -> None:
    data = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError("The service message is too large to send.")
    _write_all(handle, struct.pack(">I", len(data)) + data)


def _receive(handle: Any) -> dict[str, Any]:
    size = struct.unpack(">I", _read_exact(handle, HEADER_BYTES))[0]
    if size > MAX_MESSAGE_BYTES:
        raise ValueError("The service message is larger than the protocol allows.")
    return json.loads(_read_exact(handle, size).decode("utf-8"))


def _write_all(handle: Any, data: bytes) -> None:
    sent = 0
    while sent < len(data):
        count = wintypes.DWORD()
        chunk = data[sent:]
        if not ipc.kernel32.WriteFile(handle, chunk, len(chunk), ctypes.byref(count), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if count.value == 0:
            raise OSError("The pipe accepted no bytes.")
        sent += count.value


def _read_exact(handle: Any, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        buffer = ctypes.create_string_buffer(max(1, size - len(data)))
        count = wintypes.DWORD()
        if not ipc.kernel32.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if count.value == 0:
            raise OSError("The pipe closed before the message was complete.")
        data.extend(buffer.raw[: count.value])
    return bytes(data)


class ServiceHost:
    """The process that owns the service, serving requests until it is closed."""

    def __init__(
        self,
        service: Any,
        *,
        name: str | None = None,
        instances: int = 4,
        lock: threading.Lock | None = None,
    ) -> None:
        self.service = service
        self.name = name or pipe_name()
        self.instances = max(1, instances)
        self.lock = lock or SERVICE_LOCK
        self.stopping = threading.Event()
        self.threads: list[threading.Thread] = []
        self.handles: list[Any] = []

    def start(self) -> None:
        if os.name != "nt":
            raise PolicyError("The shared service pipe is available only on Windows.")
        attributes, descriptor = ipc._security()
        try:
            for index in range(self.instances):
                open_mode = ipc.PIPE_ACCESS_DUPLEX
                if index == 0:
                    # The first instance claims the name; a second host fails
                    # here instead of sharing the service with the first.
                    open_mode |= ipc.FILE_FLAG_FIRST_PIPE_INSTANCE
                handle = ipc.kernel32.CreateNamedPipeW(
                    self.name, open_mode,
                    # Byte mode, because the framing is ours: a message-mode read
                    # whose buffer is smaller than the message fails with
                    # ERROR_MORE_DATA, and a 12 MB screenshot would hit that.
                    ipc.PIPE_TYPE_BYTE | ipc.PIPE_READMODE_BYTE | ipc.PIPE_WAIT
                    | ipc.PIPE_REJECT_REMOTE_CLIENTS,
                    ipc.PIPE_UNLIMITED_INSTANCES, ipc.PIPE_BUFFER, ipc.PIPE_BUFFER, 0,
                    ctypes.byref(attributes),
                )
                if handle == ipc.INVALID_HANDLE_VALUE:
                    error = ctypes.get_last_error()
                    if index == 0 and error == ipc.ERROR_ACCESS_DENIED:
                        raise PolicyError(
                            "Another service host already owns the pipe; refusing to share it."
                        ) from ctypes.WinError(error)
                    raise ctypes.WinError(error)
                self.handles.append(handle)
                thread = threading.Thread(
                    target=self._serve, args=(handle,), daemon=True, name="hvmcp-service"
                )
                self.threads.append(thread)
                thread.start()
        finally:
            ipc.kernel32.LocalFree(descriptor)

    def _serve(self, handle: Any) -> None:
        try:
            while not self.stopping.is_set():
                if not ipc.kernel32.ConnectNamedPipe(handle, None) and (
                    ctypes.get_last_error() != ipc.ERROR_PIPE_CONNECTED
                ):
                    break
                if self.stopping.is_set():
                    break
                try:
                    request = _receive(handle)
                    if not isinstance(request, dict):
                        raise ValueError("A service request must be an object.")
                    response = dispatch_request(self.service, request, self.lock)
                    _send(handle, response)
                except (OSError, ValueError, json.JSONDecodeError):
                    # A malformed or abandoned request must not end this instance.
                    pass
                finally:
                    ipc.kernel32.FlushFileBuffers(handle)
                    ipc.kernel32.DisconnectNamedPipe(handle)
        finally:
            ipc.kernel32.CloseHandle(handle)

    def close(self) -> None:
        self.stopping.set()
        if os.name != "nt":
            return
        for handle in self.handles:
            ipc.kernel32.CancelIoEx(handle, None)
        for thread in self.threads:
            thread.join(timeout=2)


class ServiceClient:
    """A transport that reaches the host: one connection per call, verified.

    A connection per call is a millisecond on a local pipe and keeps no state
    that a crashed host could leave stale in the client.
    """

    def __init__(self, name: str | None = None, *, label: str = "service host") -> None:
        self.name = name or pipe_name()
        self.label = label

    def __call__(self, request: Envelope) -> Envelope:
        handle = self._connect()
        try:
            ipc._verify_server_process(handle, self.label)
            _send(handle, request)
            response = _receive(handle)
        finally:
            ipc.kernel32.CloseHandle(handle)
        if not isinstance(response, dict):
            raise PolicyError("The service host answered with something that is not a response.")
        return response

    def available(self) -> bool:
        """Whether a host is listening, without sending it anything."""
        if os.name != "nt":
            return False
        handle = self._open()
        if handle == ipc.INVALID_HANDLE_VALUE:
            return False
        ipc.kernel32.CloseHandle(handle)
        return True

    def _connect(self) -> Any:
        # Every instance is connected to another client, or created but not yet
        # waiting: both are "come back in a moment", not "no host". A name with
        # no server at all answers immediately with FILE_NOT_FOUND, which is
        # the only error worth reporting as one.
        deadline = time.monotonic() + CONNECT_TIMEOUT_MS / 1000
        while True:
            handle = self._open()
            if handle != ipc.INVALID_HANDLE_VALUE:
                return handle
            error = ctypes.get_last_error()
            remaining = deadline - time.monotonic()
            if error not in {ipc.ERROR_PIPE_BUSY, ipc.ERROR_PIPE_NOT_CONNECTED} or remaining <= 0:
                raise PolicyError(
                    "The shared Hyper-V service is not running. Start it with "
                    "`hyperv-service` (or let a client start it)."
                )
            # Losing the race to another waiter is normal; wait for the next
            # free instance rather than giving up on the first timeout.
            ipc.kernel32.WaitNamedPipeW(self.name, max(1, int(remaining * 1000)))

    def _open(self) -> Any:
        return ipc.kernel32.CreateFileW(
            self.name, ipc.GENERIC_READ | ipc.GENERIC_WRITE, 0, None,
            ipc.OPEN_EXISTING, 0, None,
        )


def transport(name: str | None = None) -> Transport:
    """A transport for the proxy, with the pipe's refusals intact."""
    return ServiceClient(name)


START_TIMEOUT_SECONDS = 20.0
_START_LOCK = threading.Lock()


def ensure_running(name: str | None = None, *, timeout: float = START_TIMEOUT_SECONDS) -> ServiceClient:
    """The client for this user's service, starting the host if nothing serves it.

    A client that finds no host starts one rather than running a second service
    inside itself: two owners would mean two workers, two serial sessions and two
    KD sessions, and a dashboard that cannot see what the AI opened.
    """
    client = ServiceClient(name)
    if client.available():
        return client
    if os.name != "nt":
        raise PolicyError("The shared service pipe is available only on Windows.")
    with _START_LOCK:
        if client.available():  # another thread in this process won the race
            return client
        _spawn()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if client.available():
                return client
            time.sleep(0.1)
    raise PolicyError(
        f"The shared Hyper-V service did not answer within {int(timeout)} seconds. "
        f"Start it by hand with `hyperv-service` and read {service_log().name} for why."
    )


def open_service(settings: Any, *, actor: str) -> tuple[Any, Any]:
    """The shared service, or a private copy when the run must not disturb one.

    The window is a client of the same host the MCP server talks to, so a KD or
    serial session the AI opens is visible there. A private copy owns its own
    backend, which is what this returns second — and why the caller has to
    close it and a proxy's caller does not.
    """
    if not settings.shared_service:
        from ..events import EVENTS, EventSink
        from ..hostwmi import create_backend
        from . import HyperVService

        backend = EventSink(create_backend(), EVENTS)
        return HyperVService(backend, settings), backend
    from .remote import ServiceProxy

    return ServiceProxy(ensure_running(), settings, actor=actor), None


def service_log() -> Path:
    from ..config import local_root
    return local_root("service") / "service.log"


def _spawn_command() -> list[str]:
    """The host as a command: a module under an interpreter, a sibling when frozen."""
    from ..frozen import HOST, is_frozen, sibling

    if is_frozen():
        return [str(sibling(HOST))]
    return [sys.executable, "-m", "hyperv_console_mcp.service.host"]


def _spawn() -> None:
    """Start the host detached, so it outlives the client that asked for it."""
    log = service_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as stream:
        subprocess.Popen(
            _spawn_command(),
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            close_fds=True,
            creationflags=(
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ),
        )


def main(argv: list[str] | None = None) -> int:
    """Run the host in the foreground, which is what a background launcher starts."""
    from ..config import Settings
    from ..events import EVENTS, EventSink
    from ..hostwmi import create_backend
    from . import HyperVService

    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] in {"-h", "--help"}:
        print("usage: hyperv-service [--pipe NAME]\n\nRuns the shared Hyper-V service host.")
        return 0
    name = None
    if len(arguments) == 2 and arguments[0] == "--pipe":
        name = arguments[1]
    # The event bus is the host's too: bridge operations publish from here, so a
    # client's activity page shows the work the AI is doing in another process.
    service = HyperVService(EventSink(create_backend(), EVENTS), Settings.from_env())
    host = ServiceHost(service, name=name)
    host.start()
    print(f"hyperv-service: serving on {host.name}", flush=True)
    try:
        host.stopping.wait()
    except KeyboardInterrupt:
        pass
    finally:
        host.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MAX_MESSAGE_BYTES",
    "ServiceClient",
    "ServiceHost",
    "dispatch_request",
    "ensure_running",
    "open_service",
    "pipe_name",
    "service_log",
    "transport",
]
