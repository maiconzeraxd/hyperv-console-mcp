from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import pytest

from hyperv_console_mcp.backend import PowerShellBackend
from hyperv_console_mcp.errors import BackendError

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell.exe") is None,
    reason="Windows PowerShell is required.",
)


def test_warning_output_does_not_break_the_response_protocol() -> None:
    backend = PowerShellBackend(Path(__file__).with_name("FakeBridge.ps1"))
    try:
        assert backend.invoke("warning", {})["call"] == 1
        assert backend.invoke("vm_list", {})["call"] == 2
    finally:
        backend.close()


def test_persistent_bridge_reuses_one_process() -> None:
    backend = PowerShellBackend(Path(__file__).with_name("FakeBridge.ps1"))
    try:
        start = time.perf_counter()
        first = backend.invoke("vm_list", {})
        cold_seconds = time.perf_counter() - start
        start = time.perf_counter()
        for _ in range(9):
            last = backend.invoke("vm_list", {})
        warm_seconds = time.perf_counter() - start
        assert first["process_id"] == last["process_id"]
        assert first["call"] == 1
        assert last["call"] == 10
        print(
            f"\nPersistent bridge benchmark: cold={cold_seconds:.3f}s; "
            f"nine warm calls={warm_seconds:.3f}s"
        )
    finally:
        backend.close()


def test_worker_crash_does_not_poison_the_next_request() -> None:
    backend = PowerShellBackend(Path(__file__).with_name("FakeBridge.ps1"))
    try:
        assert backend.invoke("vm_list", {})["call"] == 1
        with pytest.raises(BackendError, match="exited unexpectedly"):
            backend.invoke("crash", {})
        recovered = backend.invoke("vm_list", {})
        assert recovered["call"] == 1
    finally:
        backend.close()


def test_timeout_kills_worker_and_allows_a_fresh_request() -> None:
    backend = PowerShellBackend(Path(__file__).with_name("FakeBridge.ps1"))
    try:
        with pytest.raises(BackendError) as error:
            backend.invoke("slow", {}, timeout=0.05)
        assert error.value.code == "timeout"
        assert backend.invoke("vm_list", {})["call"] == 1
    finally:
        backend.close()
