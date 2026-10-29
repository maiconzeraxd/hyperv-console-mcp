"""The COM ports debug_serial_status reports, and the tools this host has."""

from __future__ import annotations

from typing import Any

import pytest

from hyperv_console_mcp.hostwmi import WmiBackend
from hyperv_console_mcp.hostwmi.connection import WmiFailure
from hyperv_console_mcp.hostwmi.serial import debug_serial_status

VM_ID = "D5AD7D1D-5CB3-441F-A9D7-138640C8F61A"
CONTROLLER = "8E3A359F-559A-4B6A-98A9-1690A6100ED7"
PIPE = r"\\.\pipe\north-test-kd"


class FakeConnection:
    def __init__(
        self, ports: list[dict[str, Any]] | None = None, computers: list[dict[str, Any]] | None = None
    ) -> None:
        self.ports = ports if ports is not None else [_port(0, PIPE, True), _port(1, "", False)]
        self.computers = [_computer()] if computers is None else computers
        self.queries: list[str] = []

    def query(self, wql: str, *, namespace: str = "") -> list[dict[str, Any]]:
        self.queries.append(wql)
        if "FROM Msvm_ComputerSystem" in wql:
            return list(self.computers)
        if "FROM Msvm_VirtualSystemManagementService" in wql:
            # The permission guard asks for the service to tell an empty host
            # from a filtered one.
            return [{"__PATH": "x"}]
        if "FROM Msvm_SerialPortSettingData" in wql:
            return list(self.ports)
        raise AssertionError(f"unexpected query: {wql}")

    def get(self, path: str, *, namespace: str = "") -> dict[str, Any]:
        raise AssertionError("listing ports should not need Get")

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        raise AssertionError("listing ports should not need a method call")


def _computer(**overrides: Any) -> dict[str, Any]:
    row = {
        "ElementName": "NORTH-Test",
        "Name": VM_ID,
        "EnabledState": 2,
        "StatusDescriptions": [],
        "OnTimeInMilliseconds": 0,
    }
    row.update(overrides)
    return row


def _port(index: int, path: str, debugger: bool) -> dict[str, Any]:
    return {
        "InstanceID": f"Microsoft:{VM_ID}\\{CONTROLLER}\\{index}",
        "ElementName": f"COM {index + 1}",
        "Connection": (path,),
        "DebuggerMode": debugger,
    }


def test_the_ports_are_numbered_from_one() -> None:
    """The InstanceID index is zero-based; the tool's number is not."""
    result = debug_serial_status(FakeConnection(), {"vm": "NORTH-Test"})
    assert result["com_ports"] == [
        {"number": 1, "path": PIPE, "debugger_mode": "On"},
        {"number": 2, "path": "", "debugger_mode": "Off"},
    ]


def test_the_ports_come_back_in_order() -> None:
    """A query has no order, and COM 2 before COM 1 would read as a bug."""
    connection = FakeConnection([_port(1, "", False), _port(0, PIPE, True)])
    numbers = [port["number"] for port in debug_serial_status(connection, {"vm": "NORTH-Test"})["com_ports"]]
    assert numbers == [1, 2]


def test_a_port_with_no_connection_reports_an_empty_path() -> None:
    """The bridge answers ``''`` here, not ``None``, and the two must match."""
    connection = FakeConnection([_port(0, "", True)])
    assert debug_serial_status(connection, {"vm": "NORTH-Test"})["com_ports"][0]["path"] == ""


def test_the_vm_state_and_tools_come_back_with_the_ports() -> None:
    result = debug_serial_status(FakeConnection(), {"vm": "NORTH-Test"})
    assert (result["vm"], result["state"]) == ("NORTH-Test", "Running")
    assert result["tools"]["kdnet"].endswith("kdnet.exe")
    assert isinstance(result["tools"]["windbgx"], bool)
    assert isinstance(result["tools"]["kdnet_installed"], bool)


def test_the_query_is_scoped_to_one_vm() -> None:
    """The class holds every VM's ports and every checkpoint's copy of them."""
    connection = FakeConnection()
    debug_serial_status(connection, {"vm": "NORTH-Test"})
    query = next(q for q in connection.queries if "FROM Msvm_SerialPortSettingData" in q)
    assert f"Microsoft:{VM_ID}" in query
    assert "ResourceType = 21" in query


def test_a_missing_vm_is_not_found() -> None:
    connection = FakeConnection(computers=[])
    with pytest.raises(WmiFailure) as caught:
        debug_serial_status(connection, {"vm": "NORTH-Gone"})
    assert caught.value.code == "not_found"


def test_the_backend_serves_debug_serial_status() -> None:
    backend = WmiBackend(connect=lambda: FakeConnection())
    assert backend.invoke("debug_serial_status", {"vm": "NORTH-Test"})["com_ports"][0]["number"] == 1
