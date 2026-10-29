"""The VM object built from CIM, checked against what Get-VM answers."""

from __future__ import annotations

from typing import Any

import pytest

from hyperv_console_mcp.errors import BackendError
from hyperv_console_mcp.hostwmi import WmiBackend
from hyperv_console_mcp.hostwmi.connection import WmiFailure
from hyperv_console_mcp.hostwmi.vm import vm_get, vm_list

VM_ID = "D5AD7D1D-5CB3-441F-A9D7-138640C8F61A"
PORT_ID = "37C7DA00-2531-4C42-89DA-3DC44FEA5D47"
SWITCH_ID = "C08CB7B8-9B3C-408E-8E30-5E16A3AEB444"


class FakeConnection:
    """Answers a query whose text contains a needle, and records the text."""

    def __init__(self, answers: list[tuple[str, list[dict[str, Any]]]]) -> None:
        self.answers = answers
        self.queries: list[str] = []

    def query(self, wql: str, *, namespace: str = "") -> list[dict[str, Any]]:
        self.queries.append(wql)
        for needle, rows in self.answers:
            if needle in wql:
                return rows
        raise AssertionError(f"unexpected query: {wql}")

    def get(self, path: str, *, namespace: str = "") -> dict[str, Any]:
        raise AssertionError("the VM mapping should not need Get")

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        raise AssertionError("the VM mapping should not need a method call")


def _computer(**overrides: Any) -> dict[str, Any]:
    row = {
        "ElementName": "NORTH-Test",
        "Name": VM_ID,
        "EnabledState": 2,
        "StatusDescriptions": ["Operando normalmente"],
        "OnTimeInMilliseconds": 28_286_151,
        "__PATH": f'\\\\HOST\\root\\virtualization\\v2:Msvm_ComputerSystem.Name="{VM_ID}"',
    }
    row.update(overrides)
    return row


def _settings(**overrides: Any) -> dict[str, Any]:
    row = {
        "ElementName": "NORTH-Test",
        "Version": "9.0",
        "VirtualSystemSubType": "Microsoft:Hyper-V:SubType:2",
        "UserSnapshotType": 5,
        "AutomaticSnapshotsEnabled": False,
        "AutomaticStartupAction": 2,
        "AutomaticShutdownAction": 3,
    }
    row.update(overrides)
    return row


def _vhd(location: int = 0) -> dict[str, Any]:
    return {
        "InstanceID": f"Microsoft:{VM_ID}\\947A833B-A3B1-45CA-9CF3-8C2746ED4D1B\\0\\{location}\\L",
        "ResourceSubType": "Microsoft:Hyper-V:Virtual Hard Disk",
        "Parent": "",
        "HostResource": [r"C:\HyperV\NORTH-Test\Virtual Hard Disks\NORTH-Test.avhdx"],
    }


def _iso(location: int, path: str | None) -> dict[str, Any]:
    return {
        "InstanceID": f"Microsoft:{VM_ID}\\947A833B-A3B1-45CA-9CF3-8C2746ED4D1B\\0\\{location}\\L",
        "ResourceSubType": "Microsoft:Hyper-V:Virtual CD/DVD Disk",
        # A reference doubles the backslashes, which is why the mapping escapes.
        "Parent": (
            '\\\\HOST\\root\\virtualization\\v2:Msvm_ResourceAllocationSettingData.InstanceID='
            f'"Microsoft:{VM_ID}\\\\947A833B-A3B1-45CA-9CF3-8C2746ED4D1B\\\\0\\\\{location}\\\\D"'
        ),
        "HostResource": [path] if path else [],
    }


def _drive(location: int) -> dict[str, Any]:
    return {
        "InstanceID": f"Microsoft:{VM_ID}\\947A833B-A3B1-45CA-9CF3-8C2746ED4D1B\\0\\{location}\\D",
        "ResourceSubType": "Microsoft:Hyper-V:Synthetic DVD Drive",
        "Parent": "",
        "HostResource": [],
    }


def _port() -> dict[str, Any]:
    return {
        "InstanceID": f"Microsoft:{VM_ID}\\{PORT_ID}",
        "ElementName": "Adaptador de Rede",
        "Address": "00155D006600",
    }


def _allocation() -> dict[str, Any]:
    return {
        "InstanceID": f"Microsoft:{VM_ID}\\{PORT_ID}\\C",
        "Parent": (
            '\\\\HOST\\root\\virtualization\\v2:Msvm_SyntheticEthernetPortSettingData.InstanceID='
            f'"Microsoft:{VM_ID}\\\\{PORT_ID}"'
        ),
        "HostResource": [
            '\\\\HOST\\root\\virtualization\\v2:Msvm_VirtualEthernetSwitch.'
            f'CreationClassName="Msvm_VirtualEthernetSwitch",Name="{SWITCH_ID}"'
        ],
    }


def _answers(**overrides: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    answers: dict[str, list[dict[str, Any]]] = {
        "FROM Msvm_ComputerSystem": [_computer()],
        "Snapshot:Realized": [{"InstanceID": "Microsoft:1"}, {"InstanceID": "Microsoft:2"}],
        "System:Realized": [_settings()],
        "FROM Msvm_MemorySettingData": [
            {"InstanceID": "m", "VirtualQuantity": 4096, "VirtualQuantityUnits": "byte * 2^20"}
        ],
        "FROM Msvm_ProcessorSettingData": [{"InstanceID": "p", "VirtualQuantity": 2}],
        "FROM Msvm_StorageAllocationSettingData": [_vhd(), _iso(1, r"C:\VMs_iso\Win10.iso")],
        "FROM Msvm_ResourceAllocationSettingData": [_drive(1)],
        "FROM Msvm_SyntheticEthernetPortSettingData": [_port()],
        "FROM Msvm_EthernetPortAllocationSettingData": [_allocation()],
        "FROM Msvm_VirtualEthernetSwitch": [{"Name": SWITCH_ID, "ElementName": "Default Switch"}],
        "DeviceID, OperationalStatus": [{"DeviceID": f"Microsoft:{PORT_ID}", "OperationalStatus": [2]}],
        "FROM Msvm_GuestNetworkAdapterConfiguration": [
            {"InstanceID": f"Microsoft:GuestNetwork\\{VM_ID}\\{PORT_ID}", "IPAddresses": ["10.0.0.5"]}
        ],
    }
    answers.update(overrides)
    return list(answers.items())


def test_vm_list_maps_what_get_vm_answers() -> None:
    result = vm_list(FakeConnection(_answers()), {})
    assert result["count"] == 1
    assert result["vms"] == [
        {
            "id": VM_ID.lower(),
            "name": "NORTH-Test",
            "state": "Running",
            "state_code": 2,
            "status": "Operando normalmente",
            "generation": 2,
            "version": "9.0",
            "processor_count": 2,
            "memory_startup_bytes": 4 * 2**30,
            "memory_assigned_bytes": 4 * 2**30,
            "uptime_seconds": 28286.151,
            "checkpoint_type": "Standard",
            "automatic_checkpoints_enabled": False,
            "automatic_start_action": "Nothing",
            "automatic_stop_action": "Save",
            "checkpoint_count": 2,
            "hard_disks": [
                {
                    "controller_number": 0,
                    "controller_location": 0,
                    "path": r"C:\HyperV\NORTH-Test\Virtual Hard Disks\NORTH-Test.avhdx",
                }
            ],
            "dvd_drives": [
                {"controller_number": 0, "controller_location": 1, "path": r"C:\VMs_iso\Win10.iso"}
            ],
            "network_adapters": [
                {
                    "name": "Adaptador de Rede",
                    "switch_name": "Default Switch",
                    "status": [2],
                    "mac_address": "00155D006600",
                    "ip_addresses": ["10.0.0.5"],
                }
            ],
        }
    ]


def test_vm_get_asks_for_the_named_vm() -> None:
    result = vm_get(FakeConnection(_answers()), {"vm": "NORTH-Test"})
    assert result["vm"]["name"] == "NORTH-Test"


def test_vm_get_reports_a_missing_vm_as_not_found() -> None:
    connection = FakeConnection(_answers())
    with pytest.raises(WmiFailure) as caught:
        vm_get(connection, {"vm": "NORTH-Gone"})
    assert caught.value.code == "not_found"
    assert str(caught.value) == "Expected exactly one VM named 'NORTH-Gone'; found 0."


def test_a_permission_filtered_host_is_not_an_empty_host() -> None:
    """The provider hides VMs from an unelevated caller instead of failing."""
    connection = FakeConnection(
        _answers(
            **{
                "FROM Msvm_ComputerSystem": [],
                "FROM Msvm_VirtualSystemManagementService": [],
            }
        )
    )
    with pytest.raises(WmiFailure) as caught:
        vm_list(connection, {})
    assert caught.value.code == "wmi_unavailable"
    assert "Hyper-V Administrator" in str(caught.value)


def test_a_host_with_no_vms_answers_an_empty_list() -> None:
    connection = FakeConnection(
        _answers(
            **{
                "FROM Msvm_ComputerSystem": [_computer(Name="HOST", ElementName="HOST")],
                "FROM Msvm_VirtualSystemManagementService": [{"__PATH": "x"}],
            }
        )
    )
    assert vm_list(connection, {}) == {"vms": [], "count": 0}


def test_a_stopped_vm_maps_its_state_and_has_no_uptime() -> None:
    connection = FakeConnection(
        _answers(**{"FROM Msvm_ComputerSystem": [_computer(EnabledState=3, OnTimeInMilliseconds=None)]})
    )
    vm = vm_list(connection, {})["vms"][0]
    assert (vm["state"], vm["state_code"], vm["uptime_seconds"]) == ("Off", 3, 0.0)


def test_a_dvd_drive_without_media_reports_no_path() -> None:
    connection = FakeConnection(
        _answers(
            **{
                "FROM Msvm_StorageAllocationSettingData": [_vhd(), _iso(1, None)],
                "FROM Msvm_ResourceAllocationSettingData": [_drive(1), _drive(2)],
            }
        )
    )
    drives = vm_list(connection, {})["vms"][0]["dvd_drives"]
    assert drives == [
        {"controller_number": 0, "controller_location": 1, "path": None},
        {"controller_number": 0, "controller_location": 2, "path": None},
    ]


def test_an_adapter_without_a_runtime_port_has_no_status() -> None:
    connection = FakeConnection(_answers(**{"DeviceID, OperationalStatus": []}))
    adapter = vm_list(connection, {})["vms"][0]["network_adapters"][0]
    assert adapter["status"] == []
    assert adapter["switch_name"] == "Default Switch"


def test_an_unknown_enum_is_named_rather_than_guessed() -> None:
    connection = FakeConnection(
        _answers(**{"System:Realized": [_settings(UserSnapshotType=99, AutomaticStartupAction=7)]})
    )
    vm = vm_list(connection, {})["vms"][0]
    assert vm["checkpoint_type"] == "Unknown"
    assert vm["automatic_start_action"] == "Unknown"


def test_the_backend_serves_vm_get_and_keeps_a_missing_vm_an_error() -> None:
    backend = WmiBackend(connect=lambda: FakeConnection(_answers()))
    assert backend.invoke("vm_get", {"vm": "NORTH-Test"})["vm"]["state"] == "Running"
    with pytest.raises(BackendError) as caught:
        backend.invoke("vm_get", {"vm": "NORTH-Gone"})
    # not_found is a real answer, so the fallback must not take the call over.
    assert caught.value.code == "not_found"
