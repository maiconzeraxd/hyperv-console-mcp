"""Current-user-only named-pipe approval broker for local MCP mutations."""

from __future__ import annotations

import ctypes
import json
import os
import threading
import uuid
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .errors import PolicyError

PIPE_BUFFER = 16384
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
PIPE_ACCESS_DUPLEX = 3
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
PIPE_TYPE_MESSAGE = 4
PIPE_READMODE_MESSAGE = 2
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
PIPE_WAIT = 0
PIPE_UNLIMITED_INSTANCES = 255
ERROR_PIPE_BUSY = 231
ERROR_PIPE_CONNECTED = 535
ERROR_ACCESS_DENIED = 5
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
PYTHON_IMAGES = frozenset({"python.exe", "pythonw.exe", "py.exe"})
MAX_SUMMARY_CHARS = 16_384

if os.name == "nt":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    ]
    kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.GetNamedPipeServerProcessId.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    kernel32.ReadFile.argtypes = kernel32.WriteFile.argtypes
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID,
    ]
    kernel32.LocalFree.argtypes = [wintypes.LPVOID]
    kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
else:
    kernel32 = None
    advapi32 = None


def _require_windows() -> None:
    if os.name != "nt":
        raise PolicyError("Local dashboard approval is available only on Windows.")


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("descriptor", wintypes.LPVOID), ("inherit", wintypes.BOOL)]


def _token_user_sid(token: wintypes.HANDLE) -> str:
    size = wintypes.DWORD()
    advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    if not advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    sid = ctypes.c_void_p(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value)
    text = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return text.value
    finally:
        kernel32.LocalFree(text)


def _current_user_sid() -> str:
    _require_windows()
    token = ctypes.c_void_p()
    kernel32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token))
    try:
        return _token_user_sid(token)
    finally:
        kernel32.CloseHandle(token)


def _pipe_name() -> str:
    return rf"\\.\pipe\HyperVConsoleMCP-Approvals-{_current_user_sid()}"


def _security() -> tuple[SecurityAttributes, wintypes.LPVOID]:
    descriptor = wintypes.LPVOID()
    # Grant only the interactive user's SID; no Everyone, anonymous, or network access.
    rule = f"D:(A;;GA;;;{_current_user_sid()})"
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(rule, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
    return attributes, descriptor


SECRET_KEY_HINTS = (
    "password", "passphrase", "secret", "token", "credential",
    "apikey", "privatekey", "kdnetkey", "hostkey",
)


def _is_secret_key(name: str) -> bool:
    normalized = name.casefold().replace("_", "").replace("-", "")
    if normalized in {"key", "keys", "pwd"}:
        return True
    return any(hint in normalized for hint in SECRET_KEY_HINTS)


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "..."
    if isinstance(value, dict):
        return {
            str(key)[:128]: _scrub(item, depth + 1)
            for key, item in list(value.items())[:64]
            if not _is_secret_key(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item, depth + 1) for item in list(value)[:64]]
    if isinstance(value, str):
        return value[:4_096]
    if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
        return value
    return str(value)[:512]


def _summary(value: Any) -> Any:
    """Bound and scrub an approval summary before it reaches the dashboard.

    Tools build secret-free summaries; this is the backstop so a stray credential
    cannot be rendered in the approval dialog even if one slips through.
    """
    cleaned = _scrub(value if isinstance(value, dict) else {})
    if len(json.dumps(cleaned, ensure_ascii=True)) > MAX_SUMMARY_CHARS:
        return {"truncated": True, "note": "The approval summary was too large to display."}
    return cleaned


def _read(handle: wintypes.HANDLE) -> dict:
    data = bytearray()
    while True:
        buffer = ctypes.create_string_buffer(PIPE_BUFFER)
        count = wintypes.DWORD()
        ok = kernel32.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None)
        data.extend(buffer.raw[: count.value])
        if len(data) > 65_536:
            raise ValueError("IPC message is too large.")
        error = ctypes.get_last_error()
        if not ok and error != 234:  # ERROR_MORE_DATA
            raise ctypes.WinError(error)
        if ok:
            break
    if len(data) > 65_536:
        raise ValueError("IPC message is too large.")
    return json.loads(data.decode("utf-8"))


def _write(handle: wintypes.HANDLE, message: dict) -> None:
    data = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    count = wintypes.DWORD()
    if not kernel32.WriteFile(handle, data, len(data), ctypes.byref(count), None) or count.value != len(data):
        raise ctypes.WinError(ctypes.get_last_error())


def _verify_server_process(handle: wintypes.HANDLE) -> None:
    """Refuse an instance that is not the expected dashboard process of this user.

    A pipe name is guessable, so the client never trusts a connection just because
    the pipe exists: the server must be a Python interpreter owned by this user.
    """
    pid = wintypes.DWORD()
    if not kernel32.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)) or not pid.value:
        raise PolicyError("The approval broker could not identify its server process.")
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not process:
        raise PolicyError("The approval broker process could not be inspected.")
    try:
        size = wintypes.DWORD(32_768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            raise PolicyError("The approval broker executable could not be identified.")
        if Path(buffer.value).name.casefold() not in PYTHON_IMAGES:
            raise PolicyError(
                "The approval broker is not the dashboard process; refusing to trust the pipe."
            )
        token = ctypes.c_void_p()
        if not kernel32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
            raise PolicyError("The approval broker token could not be inspected.")
        try:
            if _token_user_sid(token) != _current_user_sid():
                raise PolicyError("The approval broker runs as a different user.")
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(process)


def request_approval(
    operation: str, vm_name: str | None, summary: dict[str, Any] | None = None
) -> None:
    _require_windows()
    name = _pipe_name()
    handle = kernel32.CreateFileW(name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        if ctypes.get_last_error() == ERROR_PIPE_BUSY and kernel32.WaitNamedPipeW(name, 1000):
            handle = kernel32.CreateFileW(name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        raise PolicyError("Dashboard approval is required, but the local approval broker is unavailable.")
    try:
        _verify_server_process(handle)
        request = {
            "id": uuid.uuid4().hex, "operation": operation, "vm_name": vm_name,
            "summary": summary or {},
        }
        _write(handle, request)
        response = _read(handle)
        if response.get("id") != request["id"] or response.get("approved") is not True:
            raise PolicyError(f"The dashboard denied or did not approve {operation}.")
    finally:
        kernel32.CloseHandle(handle)


class ApprovalBroker:
    def __init__(self, callback: Callable[[dict], bool], instances: int = 4) -> None:
        self.callback = callback
        self.stopping = threading.Event()
        self.threads: list[threading.Thread] = []
        self.handles: list[wintypes.HANDLE] = []
        self.instances = max(1, instances)

    def start(self) -> None:
        _require_windows()
        attributes, descriptor = _security()
        try:
            for index in range(self.instances):
                # The first instance claims the name; if another broker already owns it
                # CreateNamedPipeW fails instead of silently sharing the pipe.
                open_mode = PIPE_ACCESS_DUPLEX
                if index == 0:
                    open_mode |= FILE_FLAG_FIRST_PIPE_INSTANCE
                handle = kernel32.CreateNamedPipeW(
                    _pipe_name(), open_mode,
                    PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                    PIPE_UNLIMITED_INSTANCES, PIPE_BUFFER, PIPE_BUFFER, 0, ctypes.byref(attributes),
                )
                if handle == INVALID_HANDLE_VALUE:
                    error = ctypes.get_last_error()
                    if index == 0 and error == ERROR_ACCESS_DENIED:
                        raise PolicyError(
                            "Another approval broker already owns the pipe; refusing to share it."
                        ) from ctypes.WinError(error)
                    raise ctypes.WinError(error)
                self.handles.append(handle)
                thread = threading.Thread(target=self._serve, args=(handle,), daemon=True, name="hvmcp-approval")
                self.threads.append(thread)
                thread.start()
        finally:
            kernel32.LocalFree(descriptor)

    def _serve(self, handle: wintypes.HANDLE) -> None:
        # Every instance is reused: a served connection is flushed and disconnected,
        # never closed, so the broker keeps working past the initial four approvals.
        try:
            while not self.stopping.is_set():
                if not kernel32.ConnectNamedPipe(handle, None) and ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
                    break
                if self.stopping.is_set():
                    break
                try:
                    request = _read(handle)
                    if not isinstance(request.get("id"), str) or len(request["id"]) > 64:
                        raise ValueError("Invalid request ID.")
                    approved = bool(self.callback({
                        "request_id": request["id"],
                        "operation": str(request.get("operation", ""))[:128],
                        "vm_name": str(request.get("vm_name", ""))[:256],
                        "summary": _summary(request.get("summary")),
                    }))
                    _write(handle, {"id": request["id"], "approved": approved})
                except (OSError, ValueError, json.JSONDecodeError):
                    # A malformed or abandoned request must not end this instance.
                    pass
                finally:
                    kernel32.FlushFileBuffers(handle)
                    kernel32.DisconnectNamedPipe(handle)
        finally:
            kernel32.CloseHandle(handle)

    def close(self) -> None:
        self.stopping.set()
        if os.name != "nt":
            return
        for handle in self.handles:
            kernel32.CancelIoEx(handle, None)
        for thread in self.threads:
            thread.join(timeout=2)
