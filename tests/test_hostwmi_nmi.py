"""The NMI: one method, no parameters, and a guest that may not survive it."""

from __future__ import annotations

from typing import Any

import pytest

from hyperv_console_mcp.hostwmi import WmiBackend
from hyperv_console_mcp.hostwmi.connection import WmiFailure
from hyperv_console_mcp.hostwmi.vm import vm_inject_nmi

VM_ID = "D5AD7D1D-5CB3-441F-A9D7-138640C8F61A"
VM_PATH = f'\\\\HOST\\root\\virtualization\\v2:Msvm_ComputerSystem.Name="{VM_ID}"'


class FakeConnection:
    def __init__(self, *, enabled_state: int = 2, response: dict[str, Any] | None = None) -> None:
        self.enabled_state = enabled_state
        self.response = {"ReturnValue": 0} if response is None else response
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def query(self, wql: str, *, namespace: str = "") -> list[dict[str, Any]]:
        if "FROM Msvm_ComputerSystem" in wql:
            return [
                {
                    "ElementName": "NORTH-Test",
                    "Name": VM_ID,
                    "EnabledState": self.enabled_state,
                    "StatusDescriptions": [],
                    "OnTimeInMilliseconds": 1000,
                    "__PATH": VM_PATH,
                }
            ]
        return [{"__PATH": "x"}]  # the permission guard's service query

    def get(self, path: str, *, namespace: str = "") -> dict[str, Any]:
        raise AssertionError("an NMI is not a job")

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        self.calls.append((path, method, parameters))
        return self.response


def test_the_nmi_is_one_call_on_the_computer() -> None:
    """Debug-VM is a method with no parameters; the setting data is untouched."""
    connection = FakeConnection()
    vm_inject_nmi(connection, {"vm": "NORTH-Test"})
    assert connection.calls == [(VM_PATH, "InjectNonMaskableInterrupt", {})]


def test_the_answer_names_the_vm_and_the_note() -> None:
    result = vm_inject_nmi(FakeConnection(), {"vm": "NORTH-Test"})
    assert result["vm"] == "NORTH-Test"
    assert result["injected"] is True
    assert result["state"] == "Running"
    assert "bugcheck" in result["note"]


def test_a_stopped_vm_is_refused_before_the_call() -> None:
    connection = FakeConnection(enabled_state=3)
    with pytest.raises(WmiFailure) as caught:
        vm_inject_nmi(connection, {"vm": "NORTH-Test"})
    assert caught.value.code == "invalid_state"
    assert connection.calls == []


def test_a_provider_refusal_keeps_its_code() -> None:
    connection = FakeConnection(response={"ReturnValue": 32775})
    with pytest.raises(WmiFailure) as caught:
        vm_inject_nmi(connection, {"vm": "NORTH-Test"})
    assert caught.value.code == "invalid_state"


def test_the_backend_serves_vm_inject_nmi() -> None:
    backend = WmiBackend(connect=FakeConnection)
    assert backend.invoke("vm_inject_nmi", {"vm": "NORTH-Test"})["injected"] is True
