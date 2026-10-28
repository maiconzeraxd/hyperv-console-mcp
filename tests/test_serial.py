from __future__ import annotations

import ctypes
import os
import queue
import threading
import time
import uuid
from ctypes import wintypes

import pytest
from hyperv_console_mcp.serial import SerialManager


@pytest.mark.skipif(os.name != "nt", reason="Windows named pipes are required")
def test_serial_manager_reads_writes_logs_and_disconnects(tmp_path) -> None:
    from hyperv_console_mcp import serial

    kernel32 = serial.kernel32
    kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.ConnectNamedPipe.restype = wintypes.BOOL

    pipe_name = f"hvmcp-test-{uuid.uuid4().hex}"
    pipe_path = rf"\\.\pipe\{pipe_name}"
    ready = threading.Event()
    received: queue.Queue[bytes | BaseException] = queue.Queue()

    def server() -> None:
        handle = kernel32.CreateNamedPipeW(
            pipe_path, 0x00000003, 0, 1, 4096, 4096, 0, None
        )
        if handle == serial.INVALID_HANDLE_VALUE:
            received.put(RuntimeError(f"CreateNamedPipeW failed: {ctypes.get_last_error()}"))
            ready.set()
            return
        try:
            ready.set()
            if not kernel32.ConnectNamedPipe(handle, None) and ctypes.get_last_error() != 535:
                received.put(RuntimeError(f"ConnectNamedPipe failed: {ctypes.get_last_error()}"))
                return
            output = b"guest boot\r\n"
            written = wintypes.DWORD()
            if not kernel32.WriteFile(handle, output, len(output), ctypes.byref(written), None):
                received.put(RuntimeError(f"Server WriteFile failed: {ctypes.get_last_error()}"))
                return
            buffer = ctypes.create_string_buffer(128)
            read = wintypes.DWORD()
            if not kernel32.ReadFile(handle, buffer, len(buffer), ctypes.byref(read), None):
                received.put(RuntimeError(f"Server ReadFile failed: {ctypes.get_last_error()}"))
                return
            received.put(buffer.raw[: read.value])
        finally:
            kernel32.CloseHandle(handle)

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    assert ready.wait(5)
    manager = SerialManager()
    try:
        status = manager.connect("NORTH-Test", pipe_name, str(tmp_path / "console.log"))
        assert status["connected"] is True
        deadline = time.monotonic() + 5
        while "guest boot" not in manager.read("NORTH-Test", 100)["text"]:
            if time.monotonic() >= deadline:
                pytest.fail("Timed out waiting for local serial data")
            time.sleep(0.01)
        assert manager.send("NORTH-Test", "command\r\n")["bytes_sent"] == 9
        result = received.get(timeout=5)
        if isinstance(result, BaseException):
            raise result
        assert result == b"command\r\n"
        assert "guest boot" in (tmp_path / "console.log").read_text(encoding="utf-8")
        assert manager.disconnect("NORTH-Test")["connected"] is False
    finally:
        manager.close()
        thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.skipif(os.name != "nt", reason="Windows named pipes are required")
def test_serial_send_does_not_wait_for_guest_output_and_close_cancels(tmp_path, monkeypatch) -> None:
    from hyperv_console_mcp import serial
    from hyperv_console_mcp.errors import BackendError

    monkeypatch.setattr(serial, "SERIAL_WRITE_TIMEOUT_MS", 100)

    kernel32 = serial.kernel32
    pipe_path = rf"\\.\pipe\hvmcp-silent-{uuid.uuid4().hex}"
    ready = threading.Event()
    stop = threading.Event()

    def server() -> None:
        handle = kernel32.CreateNamedPipeW(pipe_path, 0x00000003, 0, 1, 4096, 4096, 0, None)
        if handle == serial.INVALID_HANDLE_VALUE:
            ready.set()
            return
        try:
            ready.set()
            if not kernel32.ConnectNamedPipe(handle, None) and ctypes.get_last_error() != 535:
                return
            stop.wait(15)
        finally:
            kernel32.CloseHandle(handle)

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    assert ready.wait(5)
    manager = SerialManager()
    try:
        manager.connect("NORTH-Test", pipe_path.rsplit("\\", 1)[-1], str(tmp_path / "silent.log"))
        started = time.monotonic()
        assert manager.send("NORTH-Test", "command\r\n")["bytes_sent"] == 9
        assert time.monotonic() - started < 5
        with pytest.raises(BackendError) as error:
            manager.send("NORTH-Test", "x" * (512 * 1024))
        assert error.value.code == "serial_write_timeout"
        pending: queue.Queue[BaseException] = queue.Queue()

        def send_during_close() -> None:
            try:
                manager.send("NORTH-Test", "y" * (512 * 1024))
            except BackendError as exc:
                pending.put(exc)

        sender = threading.Thread(target=send_during_close, daemon=True)
        sender.start()
        time.sleep(0.1)
        started = time.monotonic()
        manager.disconnect("NORTH-Test")
        sender.join(timeout=5)
        assert not sender.is_alive()
        assert isinstance(pending.get_nowait(), BackendError)
        assert time.monotonic() - started < 5
    finally:
        stop.set()
        manager.close()
        thread.join(timeout=5)
    assert not thread.is_alive()
