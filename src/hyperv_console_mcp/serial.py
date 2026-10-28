"""Dedicated Windows named-pipe readers for Hyper-V COM port consoles."""

from __future__ import annotations

import atexit
import codecs
import ctypes
import os
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .errors import BackendError

_BUFFER_CHARACTERS = 1_048_576
SERIAL_WRITE_TIMEOUT_MS = 10_000


class _SerialSession:
    def __init__(self, pipe_name: str, log_path: str) -> None:
        self.pipe_path = rf"\\.\pipe\{pipe_name}"
        self.log_path = str(Path(log_path).resolve())
        self._buffer = ""
        self._error: str | None = None
        self._closing = False
        self._buffer_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._handle = self._open_pipe()
        try:
            self._log = open(self.log_path, "a", encoding="utf-8", newline="")
        except Exception:
            kernel32.CloseHandle(self._handle)
            raise
        self._reader = threading.Thread(
            target=self._read_loop, daemon=True, name=f"hvmcp-serial-{pipe_name}"
        )
        self._reader.start()

    def _open_pipe(self) -> int:
        if os.name != "nt":
            raise BackendError(
                "Hyper-V serial consoles are available only on Windows.", code="serial_unavailable"
            )
        handle = kernel32.CreateFileW(
            self.pipe_path, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None
        )
        if handle == INVALID_HANDLE_VALUE:
            error = ctypes.get_last_error()
            if error == ERROR_PIPE_BUSY:
                kernel32.WaitNamedPipeW(self.pipe_path, 5_000)
                handle = kernel32.CreateFileW(
                    self.pipe_path, GENERIC_READ | GENERIC_WRITE, 0, None,
                    OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None,
                )
                error = ctypes.get_last_error()
        if handle == INVALID_HANDLE_VALUE:
            raise BackendError(
                f"Could not open {self.pipe_path} (Windows error {error}). "
                "Check the VM COM port, VM state, elevation, and whether another debugger owns the pipe.",
                code="serial_connect_failed",
            )
        return int(handle)

    def _read_loop(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        chunk = ctypes.create_string_buffer(4096)
        event = kernel32.CreateEventW(None, True, False, None)
        if not event:
            self._error = "Could not create the serial read event."
            return
        try:
            while True:
                overlapped = Overlapped(event=event)
                read = wintypes.DWORD()
                success = kernel32.ReadFile(
                    self._handle, chunk, len(chunk), ctypes.byref(read), ctypes.byref(overlapped)
                )
                if not success:
                    error = ctypes.get_last_error()
                    if error == ERROR_IO_PENDING:
                        success = kernel32.GetOverlappedResult(
                            self._handle, ctypes.byref(overlapped), ctypes.byref(read), True
                        )
                        error = ctypes.get_last_error()
                    if not success:
                        if not self._closing and error not in {ERROR_BROKEN_PIPE, ERROR_OPERATION_ABORTED}:
                            self._error = f"Serial ReadFile failed with Windows error {error}."
                        break
                if not read.value:
                    continue
                text = decoder.decode(chunk.raw[: read.value])
                with self._buffer_lock:
                    self._buffer = (self._buffer + text)[-_BUFFER_CHARACTERS:]
                self._log.write(text)
                self._log.flush()
        except Exception as exc:
            self._error = f"Serial reader failed ({type(exc).__name__})."
        finally:
            kernel32.CloseHandle(event)
            try:
                self._log.close()
            except Exception:
                pass

    def read(self, tail_characters: int) -> dict[str, Any]:
        with self._buffer_lock:
            text = self._buffer[-tail_characters:]
            buffered = len(self._buffer)
        return {
            "text": text,
            "characters": len(text),
            "buffered_characters": buffered,
            "buffer_truncated": buffered > len(text),
            **self.status(),
        }

    def send(self, text: str) -> int:
        data = text.encode("utf-8")
        with self._write_lock:
            with self._state_lock:
                if self._closing:
                    raise BackendError("The serial session is disconnecting.", code="serial_not_connected")
                if self._error or not self._reader.is_alive():
                    raise BackendError(
                        self._error or "The serial pipe is no longer connected.", code="serial_disconnected"
                    )
            event = kernel32.CreateEventW(None, True, False, None)
            if not event:
                raise BackendError("Could not create the serial write event.", code="serial_write_failed")
            try:
                offset = 0
                while offset < len(data):
                    with self._state_lock:
                        if self._closing:
                            raise BackendError("The serial session is disconnecting.", code="serial_not_connected")
                    overlapped = Overlapped(event=event)
                    written = wintypes.DWORD()
                    success = kernel32.WriteFile(
                        self._handle, data[offset:], len(data) - offset,
                        ctypes.byref(written), ctypes.byref(overlapped),
                    )
                    if not success and ctypes.get_last_error() == ERROR_IO_PENDING:
                        if kernel32.WaitForSingleObject(event, SERIAL_WRITE_TIMEOUT_MS) != WAIT_OBJECT_0:
                            kernel32.CancelIoEx(self._handle, ctypes.byref(overlapped))
                            kernel32.GetOverlappedResult(
                                self._handle, ctypes.byref(overlapped), ctypes.byref(written), True
                            )
                            raise BackendError("The serial write timed out after 10 seconds.", code="serial_write_timeout")
                        success = kernel32.GetOverlappedResult(
                            self._handle, ctypes.byref(overlapped), ctypes.byref(written), True
                        )
                    if not success or not written.value:
                        error = ctypes.get_last_error()
                        raise BackendError(
                            f"Serial WriteFile failed with Windows error {error}.", code="serial_write_failed"
                        )
                    offset += written.value
            finally:
                kernel32.CloseHandle(event)
        return len(data)

    def status(self) -> dict[str, Any]:
        with self._buffer_lock:
            buffered = len(self._buffer)
        return {
            "pipe_path": self.pipe_path,
            "log_path": self.log_path,
            "connected": not self._closing and self._reader.is_alive() and self._error is None,
            "buffered_characters": buffered,
            "reader_error": self._error,
        }

    def close(self) -> None:
        with self._state_lock:
            if self._closing:
                return
            self._closing = True
        kernel32.CancelIoEx(self._handle, None)
        self._reader.join(timeout=3)
        with self._write_lock:
            kernel32.CloseHandle(self._handle)


class SerialManager:
    """Own one cancellable reader thread per VM."""

    def __init__(self) -> None:
        self._sessions: dict[str, _SerialSession] = {}
        self._lock = threading.Lock()
        atexit.register(self.close)

    @staticmethod
    def _key(vm_name: str) -> str:
        return vm_name.casefold()

    def connect(self, vm_name: str, pipe_name: str, log_path: str) -> dict[str, Any]:
        key = self._key(vm_name)
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                status = existing.status()
                if status["connected"]:
                    raise BackendError(
                        "A serial session is already attached to this VM.", code="serial_already_connected"
                    )
                existing.close()
                del self._sessions[key]
            try:
                session = _SerialSession(pipe_name, log_path)
            except OSError as exc:
                raise BackendError(
                    f"Could not open the serial log: {exc}", code="serial_log_failed"
                ) from None
            self._sessions[key] = session
            return session.status()

    def _session(self, vm_name: str) -> _SerialSession:
        with self._lock:
            session = self._sessions.get(self._key(vm_name))
        if session is None:
            raise BackendError(
                "No serial session is attached to this VM.", code="serial_not_connected"
            )
        return session

    def status(self, vm_name: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(self._key(vm_name))
        return session.status() if session is not None else {"connected": False}

    def read(self, vm_name: str, tail_characters: int) -> dict[str, Any]:
        return self._session(vm_name).read(tail_characters)

    def send(self, vm_name: str, text: str) -> dict[str, Any]:
        return {"bytes_sent": self._session(vm_name).send(text), "completed": True}

    def disconnect(self, vm_name: str) -> dict[str, Any]:
        key = self._key(vm_name)
        with self._lock:
            session = self._sessions.pop(key, None)
        if session is None:
            return {"connected": False, "completed": True}
        session.close()
        return {"connected": False, "completed": True}

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass


if os.name == "nt":
    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

        def __init__(self, *, event: wintypes.HANDLE) -> None:
            super().__init__()
            self.hEvent = event

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.WaitNamedPipeW.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = kernel32.ReadFile.argtypes
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(Overlapped)]
    kernel32.CancelIoEx.restype = wintypes.BOOL
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.GetOverlappedResult.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(Overlapped), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
    ]
    kernel32.GetOverlappedResult.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
WAIT_OBJECT_0 = 0
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_PIPE_BUSY = 231
ERROR_BROKEN_PIPE = 109
ERROR_OPERATION_ABORTED = 995
ERROR_IO_PENDING = 997
