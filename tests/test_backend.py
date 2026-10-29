from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from hyperv_console_mcp.backend import GuestSplitBackend, PowerShellBackend
from hyperv_console_mcp.errors import BackendError


class Recorder:
    """A backend that records what reached it, and can block a guest call."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []
        self.release = None

    def invoke(self, operation, payload, *, timeout=None):
        if self.release is not None:
            self.release.wait(5)
        self.calls.append(operation)
        return {"worker": self.name, "operation": operation}


def test_guest_operations_use_their_own_worker() -> None:
    host, guest = Recorder("host"), Recorder("guest")
    backend = GuestSplitBackend(host=host, guest=guest)
    assert backend.invoke("vm_get", {"vm": "LAB"})["worker"] == "host"
    assert backend.invoke("guest_exec", {"vm": "LAB"})["worker"] == "guest"
    assert backend.invoke("ssh_network_prepare", {"vm": "LAB"})["worker"] == "host"
    assert host.calls == ["vm_get", "ssh_network_prepare"]
    assert guest.calls == ["guest_exec"]


def test_a_slow_guest_call_does_not_block_the_host_worker() -> None:
    import threading

    host, guest = Recorder("host"), Recorder("guest")
    guest.release = threading.Event()
    backend = GuestSplitBackend(host=host, guest=guest)
    slow = threading.Thread(target=lambda: backend.invoke("guest_exec", {"vm": "LAB"}))
    slow.start()
    try:
        assert backend.invoke("vm_get", {"vm": "LAB"})["worker"] == "host"
    finally:
        guest.release.set()
        slow.join(5)
    assert guest.calls == ["guest_exec"]


def test_close_closes_both_workers() -> None:
    class Closable(Recorder):
        def __init__(self, name: str) -> None:
            super().__init__(name)
            self.closed = False

        def close(self) -> None:
            self.closed = True

    host, guest = Closable("host"), Closable("guest")
    GuestSplitBackend(host=host, guest=guest).close()
    assert host.closed and guest.closed


def test_bridge_response_is_base64_json() -> None:
    value = {"ok": True, "result": {"vms": [], "message": "Olá"}}
    line = "HVMCP1:" + base64.b64encode(json.dumps(value).encode()).decode()
    assert PowerShellBackend._decode("noise\n" + line) == value


def test_bridge_rejects_malformed_response() -> None:
    with pytest.raises(BackendError):
        PowerShellBackend._decode("HVMCP1:not-base64")
    assert PowerShellBackend._decode("unstructured") is None


def test_bridge_missing_script() -> None:
    with pytest.raises(BackendError):
        PowerShellBackend(Path("does-not-exist.ps1"))
