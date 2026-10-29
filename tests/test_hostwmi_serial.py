"""The COM ports debug_serial_status reports, and the tools this host has."""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree

import pytest

from hyperv_console_mcp.hostwmi.backend import WmiBackend
from hyperv_console_mcp.hostwmi.connection import WmiFailure
from hyperv_console_mcp.hostwmi.serial import (
    debug_serial_prepare,
    debug_serial_status,
    suggested_pipe_name,
)

VM_ID = "D5AD7D1D-5CB3-441F-A9D7-138640C8F61A"
CONTROLLER = "8E3A359F-559A-4B6A-98A9-1690A6100ED7"
PIPE = r"\\.\pipe\north-test-kd"
OFF = 3


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
    """The class holds every VM's ports.

    A checkpoint's copy is keyed by the checkpoint's identifier, not the VM's,
    so the same prefix keeps those out too.
    """
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


class PrepareConnection(FakeConnection):
    """A stopped VM whose COM1 is free, and a provider that applies the change."""

    def __init__(self, state: int = OFF, *, apply: bool = True) -> None:
        super().__init__(computers=[_computer(EnabledState=state)])
        self.ports = [_port(0, "", False), _port(1, "", False)]
        self.apply = apply
        self.calls: list[dict[str, Any]] = []

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        self.calls.append({"path": path, "method": method, **parameters})
        if self.apply:
            for port in self.ports:
                if port["InstanceID"] == _value(parameters["ResourceSettings"][0], "InstanceID"):
                    port["Connection"] = (_connection(parameters["ResourceSettings"][0]),)
                    port["DebuggerMode"] = True
        return {"ReturnValue": 0}


def _value(xml: str, name: str) -> str:
    for prop in ElementTree.fromstring(xml):
        if prop.get("NAME") == name:
            element = prop.find("VALUE")
            return "" if element is None else (element.text or "")
    raise AssertionError(f"the embedded instance carries no {name}")


def _connection(xml: str) -> str:
    for prop in ElementTree.fromstring(xml):
        if prop.get("NAME") == "Connection":
            element = prop.find("VALUE.ARRAY/VALUE")
            return "" if element is None else (element.text or "")
    raise AssertionError("the embedded instance carries no Connection")


def test_prepare_points_com1_at_the_pipe() -> None:
    result = debug_serial_prepare(PrepareConnection(), {"vm": "NORTH-Test", "pipe_name": "north-test-kd"})
    assert result["pipe_path"] == PIPE
    assert (result["port_number"], result["debugger_mode"]) == (1, "On")
    assert result["baud_rate"] == 115200
    assert result["windbg_arguments"] == (
        r"-k com:pipe,port=\\.\pipe\north-test-kd,baud=115200,reconnect,resets=0"
    )
    assert len(result["next_steps"]) == 4


def test_prepare_modifies_the_ports_own_setting() -> None:
    """COM2 is a different setting; changing it would leave COM1 as it was."""
    connection = PrepareConnection()
    debug_serial_prepare(connection, {"vm": "NORTH-Test", "pipe_name": "north-test-kd"})
    xml = connection.calls[0]["ResourceSettings"][0]
    assert _value(xml, "InstanceID") == f"Microsoft:{VM_ID}\\{CONTROLLER}\\0"


def test_prepare_sends_the_connection_as_an_array() -> None:
    """As a scalar the provider refuses the setting, which is what a disc taught."""
    connection = PrepareConnection()
    debug_serial_prepare(connection, {"vm": "NORTH-Test", "pipe_name": "north-test-kd"})
    xml = connection.calls[0]["ResourceSettings"][0]
    assert 'PROPERTY.ARRAY NAME="Connection"' in xml
    assert "<VALUE.ARRAY>" in xml
    assert _connection(xml) == PIPE


def test_prepare_refuses_a_running_vm() -> None:
    connection = PrepareConnection(state=2)
    with pytest.raises(WmiFailure) as caught:
        debug_serial_prepare(connection, {"vm": "NORTH-Test", "pipe_name": "north-test-kd"})
    assert caught.value.code == "invalid_state"
    assert connection.calls == []


@pytest.mark.parametrize(
    "name",
    ["", "!!!abc", "north test", "north/test", "north\\test", "-leading", "a" * 65],
)
def test_prepare_refuses_a_pipe_name_that_is_not_a_name(name: str) -> None:
    """The bridge's own regex was unanchored, so ``!!!abc`` reached Hyper-V."""
    connection = PrepareConnection()
    with pytest.raises(WmiFailure) as caught:
        debug_serial_prepare(connection, {"vm": "NORTH-Test", "pipe_name": name})
    assert caught.value.code == "invalid_argument"
    assert connection.calls == []


def test_prepare_fails_when_the_port_did_not_change() -> None:
    """A provider that accepts the call and changes nothing is a failure, not a success."""
    connection = PrepareConnection(apply=False)
    with pytest.raises(WmiFailure) as caught:
        debug_serial_prepare(connection, {"vm": "NORTH-Test", "pipe_name": "north-test-kd"})
    assert caught.value.code == "backend_error"
    assert "COM1 was not changed" in str(caught.value)


def test_the_backend_serves_debug_serial_prepare() -> None:
    backend = WmiBackend(connect=PrepareConnection)
    result = backend.invoke("debug_serial_prepare", {"vm": "NORTH-Test", "pipe_name": "north-test-kd"})
    assert result["pipe_path"] == PIPE


def test_the_pipe_name_is_derived_from_the_vm() -> None:
    """A fixed pipe name is a guess about someone else's host."""
    assert suggested_pipe_name("NORTH-Test") == "north-test-kd"
    assert suggested_pipe_name("Lab VM Ñandú") == "lab-vm-and-kd"
    assert suggested_pipe_name("---") == "vm-kd"


@pytest.mark.parametrize("vm_name", ["NORTH-Test", "Lab VM Ñandú", "---", "x" * 200, "a b.c_d-e"])
def test_a_derived_pipe_name_is_one_the_provider_accepts(vm_name: str) -> None:
    """The name goes through the same regex a caller-supplied one does."""
    result = debug_serial_prepare(
        PrepareConnection(), {"vm": "NORTH-Test", "pipe_name": suggested_pipe_name(vm_name)}
    )
    assert result["debugger_mode"] == "On"
    assert len(suggested_pipe_name(vm_name)) <= 64

