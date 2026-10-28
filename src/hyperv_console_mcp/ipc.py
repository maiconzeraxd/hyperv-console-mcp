"""Current-user-only named-pipe approval broker for local MCP mutations."""

from __future__ import annotations

import ctypes
import json
import os
import threading
import uuid
from collections.abc import Callable
from ctypes import wintypes

from .errors import PolicyError

PIPE_BUFFER = 16384
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
PIPE_ACCESS_DUPLEX = 3
PIPE_TYPE_MESSAGE = 4
PIPE_READMODE_MESSAGE = 2
PIPE_WAIT = 0
PIPE_UNLIMITED_INSTANCES = 255
ERROR_PIPE_BUSY = 231
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

if os.name == "nt":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    ]
    kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
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
    kernel32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
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


def _current_user_sid() -> str:
    _require_windows()
    token = ctypes.c_void_p()
    kernel32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token))
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.c_void_p(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value)
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        value = text.value
        kernel32.LocalFree(text)
        return value
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


def request_approval(operation: str, vm_name: str | None) -> None:
    _require_windows()
    name = _pipe_name()
    handle = kernel32.CreateFileW(name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        if ctypes.get_last_error() == ERROR_PIPE_BUSY and kernel32.WaitNamedPipeW(name, 1000):
            handle = kernel32.CreateFileW(name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        raise PolicyError("Dashboard approval is required, but the local approval broker is unavailable.")
    try:
        request = {"id": uuid.uuid4().hex, "operation": operation, "vm_name": vm_name}
        _write(handle, request)
        response = _read(handle)
        if response.get("id") != request["id"] or response.get("approved") is not True:
            raise PolicyError(f"The dashboard denied or did not approve {operation}.")
    finally:
        kernel32.CloseHandle(handle)


class ApprovalBroker:
    def __init__(self, callback: Callable[[dict], bool]) -> None:
        self.callback = callback
        self.stopping = threading.Event()
        self.threads: list[threading.Thread] = []
        self.handles: list[wintypes.HANDLE] = []

    def start(self) -> None:
        _require_windows()
        attributes, descriptor = _security()
        try:
            for _ in range(4):
                handle = kernel32.CreateNamedPipeW(
                    _pipe_name(), PIPE_ACCESS_DUPLEX, PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
                    PIPE_UNLIMITED_INSTANCES, PIPE_BUFFER, PIPE_BUFFER, 0, ctypes.byref(attributes),
                )
                if handle == INVALID_HANDLE_VALUE:
                    raise ctypes.WinError(ctypes.get_last_error())
                self.handles.append(handle)
                thread = threading.Thread(target=self._serve, args=(handle,), daemon=True, name="hvmcp-approval")
                self.threads.append(thread)
                thread.start()
        finally:
            kernel32.LocalFree(descriptor)

    def _serve(self, handle: wintypes.HANDLE) -> None:
        while not self.stopping.is_set():
            if not kernel32.ConnectNamedPipe(handle, None) and ctypes.get_last_error() != 535:
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
                }))
                _write(handle, {"id": request["id"], "approved": approved})
            except (OSError, ValueError, json.JSONDecodeError):
                break
        kernel32.CloseHandle(handle)

    def close(self) -> None:
        self.stopping.set()
        if os.name != "nt":
            return
        for handle in self.handles:
            kernel32.CancelIoEx(handle, None)
        for thread in self.threads:
            thread.join(timeout=2)
