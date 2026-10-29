"""The DVD drives media_list reports, from the drive and disc settings."""

from __future__ import annotations

from typing import Any

import pytest

from hyperv_console_mcp.errors import BackendError
from hyperv_console_mcp.hostwmi import WmiBackend
from hyperv_console_mcp.hostwmi.connection import WmiFailure
from hyperv_console_mcp.hostwmi.media import media_list

VM_ID = "D5AD7D1D-5CB3-441F-A9D7-138640C8F61A"
CONTROLLER = "947A833B-A3B1-45CA-9CF3-8C2746ED4D1B"


class FakeConnection:
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
        raise AssertionError("listing media should not need Get")

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        raise AssertionError("listing media should not need a method call")


def _computer(**overrides: Any) -> dict[str, Any]:
    row = {
        "ElementName": "NORTH-Test",
        "Name": VM_ID,
        "EnabledState": 3,
        "StatusDescriptions": [],
        "OnTimeInMilliseconds": 0,
    }
    row.update(overrides)
    return row


def _settings() -> dict[str, Any]:
    return {"VirtualSystemSubType": "Microsoft:Hyper-V:SubType:2", "Version": "9.0"}


def _drive(location: int) -> dict[str, Any]:
    return {
        "InstanceID": f"Microsoft:{VM_ID}\\{CONTROLLER}\\0\\{location}\\D",
        "ResourceSubType": "Microsoft:Hyper-V:Synthetic DVD Drive",
        "Parent": "",
        "HostResource": [],
    }


def _disc(location: int, path: str | None) -> dict[str, Any]:
    return {
        "InstanceID": f"Microsoft:{VM_ID}\\{CONTROLLER}\\0\\{location}\\L",
        "ResourceSubType": "Microsoft:Hyper-V:Virtual CD/DVD Disk",
        "Parent": (
            '\\\\HOST\\root\\virtualization\\v2:Msvm_ResourceAllocationSettingData.InstanceID='
            f'"Microsoft:{VM_ID}\\\\{CONTROLLER}\\\\0\\\\{location}\\\\D"'
        ),
        "HostResource": [path] if path else [],
    }


def _answers(**overrides: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    answers: dict[str, list[dict[str, Any]]] = {
        "FROM Msvm_ComputerSystem": [_computer()],
        "System:Realized": [_settings()],
        "FROM Msvm_StorageAllocationSettingData": [
            _disc(1, r"C:\VMs_iso\Win10.iso"),
            _disc(2, None),
        ],
        "FROM Msvm_ResourceAllocationSettingData": [_drive(1), _drive(2)],
    }
    answers.update(overrides)
    return list(answers.items())


def test_media_list_maps_what_get_vmdvddrive_answers() -> None:
    result = media_list(FakeConnection(_answers()), {"vm": "NORTH-Test"})
    assert result == {
        "vm": "NORTH-Test",
        "state": "Off",
        "generation": 2,
        "dvd_drives": [
            {
                "controller_number": 0,
                "controller_location": 1,
                "path": r"C:\VMs_iso\Win10.iso",
                "mounted": True,
            },
            {
                "controller_number": 0,
                "controller_location": 2,
                "path": None,
                "mounted": False,
            },
        ],
    }


def test_a_blank_disc_path_is_not_a_mounted_disc() -> None:
    """The bridge treats whitespace as empty, so the mapping has to as well."""
    connection = FakeConnection(
        _answers(
            **{
                "FROM Msvm_StorageAllocationSettingData": [_disc(1, "   ")],
                "FROM Msvm_ResourceAllocationSettingData": [_drive(1)],
            }
        )
    )
    drive = media_list(connection, {"vm": "NORTH-Test"})["dvd_drives"][0]
    assert (drive["path"], drive["mounted"]) == ("   ", False)


def test_the_listing_reads_only_the_classes_it_needs() -> None:
    """Six projected queries would answer this; two of them are enough."""
    connection = FakeConnection(_answers())
    media_list(connection, {"vm": "NORTH-Test"})
    classes = [query.split("FROM ")[1].split()[0] for query in connection.queries]
    assert classes == [
        "Msvm_ComputerSystem",
        "Msvm_VirtualSystemSettingData",
        "Msvm_StorageAllocationSettingData",
        "Msvm_ResourceAllocationSettingData",
    ]


def test_a_running_vm_still_lists_its_media() -> None:
    """Listing is read-only, so the Off precondition belongs to mount and eject."""
    connection = FakeConnection(_answers(**{"FROM Msvm_ComputerSystem": [_computer(EnabledState=2)]}))
    assert media_list(connection, {"vm": "NORTH-Test"})["state"] == "Running"


def test_a_missing_vm_is_not_found() -> None:
    connection = FakeConnection(
        _answers(
            **{
                "FROM Msvm_ComputerSystem": [],
                # The permission guard asks for the service to tell an empty host
                # from a filtered one; here the account can see it.
                "FROM Msvm_VirtualSystemManagementService": [{"__PATH": "x"}],
            }
        )
    )
    with pytest.raises(WmiFailure) as caught:
        media_list(connection, {"vm": "NORTH-Gone"})
    assert caught.value.code == "not_found"


def test_the_backend_serves_media_list() -> None:
    backend = WmiBackend(connect=lambda: FakeConnection(_answers()))
    assert backend.invoke("media_list", {"vm": "NORTH-Test"})["dvd_drives"][0]["mounted"] is True


def test_changing_media_is_still_the_workers() -> None:
    backend = WmiBackend(connect=lambda: FakeConnection(_answers()))
    with pytest.raises(BackendError) as caught:
        backend.invoke("media_mount", {"vm": "NORTH-Test", "path": "x.iso"})
    assert caught.value.code == "wmi_unsupported"
