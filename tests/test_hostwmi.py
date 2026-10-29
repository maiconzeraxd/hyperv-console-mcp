"""The WMI host path: mapping without a hypervisor, refusal without COM."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from hyperv_console_mcp.backend import FallbackBackend, PowerShellBackend
from hyperv_console_mcp.errors import BackendError
from hyperv_console_mcp.hostwmi import WmiBackend, create_backend
from hyperv_console_mcp.hostwmi.connection import (
    CIMV2_NAMESPACE,
    ComConnection,
    WmiFailure,
    wql_literal,
)
from hyperv_console_mcp.hostwmi.host import host_status


class FakeConnection:
    """Answers queries by prefix and remembers what was asked."""

    def __init__(self, answers: dict[str, list[dict[str, Any]]]) -> None:
        self.answers = answers
        self.queries: list[tuple[str, str]] = []

    def query(self, wql: str, *, namespace: str = "") -> list[dict[str, Any]]:
        self.queries.append((namespace, wql))
        for prefix, rows in self.answers.items():
            if wql.startswith(prefix):
                return rows
        raise AssertionError(f"unexpected query: {wql}")

    def get(self, path: str, *, namespace: str = "") -> dict[str, Any]:
        raise AssertionError("host_status should not need Get")

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        raise AssertionError("host_status should not need a method call")


COMPUTER = "SELECT Name, HypervisorPresent"
SERVICES = "SELECT Name, State, StartMode"


def _connection(computer: list[dict[str, Any]] | None = None) -> FakeConnection:
    return FakeConnection(
        {
            COMPUTER: computer
            if computer is not None
            else [
                {
                    "Name": "HOST-1",
                    "HypervisorPresent": True,
                    "NumberOfLogicalProcessors": 12,
                    "TotalPhysicalMemory": 34359738368,
                }
            ],
            SERVICES: [
                {"Name": "vmms", "State": "Running", "StartMode": "Auto"},
                {"Name": "vmcompute", "State": "Running", "StartMode": "Auto"},
            ],
        }
    )


def test_host_status_maps_the_two_cim_classes() -> None:
    connection = _connection()
    assert host_status(connection, {}) == {
        "computer_name": "HOST-1",
        "hypervisor_present": True,
        "logical_processor_count": 12,
        "memory_capacity_bytes": 34359738368,
        "services": [
            {"name": "vmms", "state": "Running", "start_mode": "Auto"},
            {"name": "vmcompute", "state": "Running", "start_mode": "Auto"},
        ],
    }
    assert [namespace for namespace, _ in connection.queries] == [CIMV2_NAMESPACE] * 2


def test_host_status_accepts_a_missing_hypervisor_flag() -> None:
    result = host_status(_connection([{"Name": "HOST-2", "TotalPhysicalMemory": None}]), {})
    assert result["hypervisor_present"] is False
    assert result["logical_processor_count"] == 0
    assert result["memory_capacity_bytes"] == 0


def test_host_status_refuses_an_ambiguous_computer_row() -> None:
    with pytest.raises(WmiFailure):
        host_status(_connection([{"Name": "a"}, {"Name": "b"}]), {})


def test_wql_literal_escapes_backslash_and_quote() -> None:
    assert wql_literal(r"C:\VMs\o'brien") == r"'C:\\VMs\\o''brien'"


def test_a_migrated_operation_reaches_the_handler() -> None:
    connection = _connection()
    backend = WmiBackend(connect=lambda: connection)
    result = backend.invoke("host_status", {"unused": True})
    assert result["computer_name"] == "HOST-1"
    assert connection.queries


def test_an_unmigrated_operation_is_refused_as_unsupported() -> None:
    backend = WmiBackend(connect=lambda: _connection())
    with pytest.raises(BackendError) as caught:
        # The PowerShell job registry has no CIM equivalent, so this operation
        # stays on the worker for good — a stable example of "not migrated".
        backend.invoke("operation_status", {"operation_id": "0" * 32})
    assert caught.value.code == "wmi_unsupported"


def test_constructing_the_backend_never_touches_com() -> None:
    def explode() -> None:
        raise AssertionError("COM was opened while constructing the backend")

    backend = WmiBackend(connect=explode)
    with pytest.raises(BackendError) as caught:
        backend.invoke("operation_status", {})
    assert caught.value.code == "wmi_unsupported"


def test_a_wmi_failure_keeps_its_code() -> None:
    def failing(_wql: str, *, namespace: str = "") -> list[dict[str, Any]]:
        raise WmiFailure("the provider is angry", code="wmi_unavailable")

    connection = FakeConnection({})
    connection.query = failing  # type: ignore[method-assign]
    backend = WmiBackend(connect=lambda: connection)
    with pytest.raises(BackendError) as caught:
        backend.invoke("host_status", {})
    assert caught.value.code == "wmi_unavailable"
    assert str(caught.value) == "the provider is angry"


def test_missing_pywin32_reports_itself_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pythoncom", None)
    with pytest.raises(WmiFailure) as caught:
        ComConnection()
    assert caught.value.code == "wmi_unavailable"
    assert "pywin32" in str(caught.value)


class Recorder:
    """A backend that records the call and either answers or raises."""

    def __init__(self, name: str, error: BackendError | None = None) -> None:
        self.name = name
        self.error = error
        self.calls: list[str] = []

    def invoke(self, operation, payload, *, timeout=None):
        self.calls.append(operation)
        if self.error is not None:
            raise self.error
        return {"worker": self.name, "operation": operation}


@pytest.mark.parametrize("code", ["wmi_unsupported", "wmi_unavailable"])
def test_the_fallback_takes_over_when_the_primary_refuses(code: str) -> None:
    primary = Recorder("wmi", BackendError("not yet", code=code))
    fallback = Recorder("powershell")
    result = FallbackBackend(primary, fallback).invoke("vm_get", {"vm": "NORTH-Test"})
    assert result == {"worker": "powershell", "operation": "vm_get"}
    assert primary.calls == ["vm_get"]


def test_the_fallback_leaves_a_real_failure_alone() -> None:
    primary = Recorder("wmi", BackendError("access denied", code="backend_error"))
    fallback = Recorder("powershell")
    with pytest.raises(BackendError) as caught:
        FallbackBackend(primary, fallback).invoke("vm_get", {})
    assert caught.value.code == "backend_error"
    assert fallback.calls == []


def test_a_primary_that_answers_is_not_repeated() -> None:
    primary = Recorder("wmi")
    fallback = Recorder("powershell")
    result = FallbackBackend(primary, fallback).invoke("host_status", {})
    assert result == {"worker": "wmi", "operation": "host_status"}
    assert fallback.calls == []


def test_close_reaches_both_workers() -> None:
    closed: list[str] = []

    class Closer(Recorder):
        def close(self) -> None:
            closed.append(self.name)

    FallbackBackend(Closer("wmi"), Closer("powershell")).close()
    assert closed == ["wmi", "powershell"]


def test_create_backend_routes_host_through_wmi_first() -> None:
    backend = create_backend()
    assert isinstance(backend.host, FallbackBackend)
    assert isinstance(backend.host.primary, WmiBackend)
    assert isinstance(backend.host.fallback, PowerShellBackend)
    # The guest side stays on PowerShell Direct; WMI cannot reach into it.
    assert isinstance(backend.guest, PowerShellBackend)
