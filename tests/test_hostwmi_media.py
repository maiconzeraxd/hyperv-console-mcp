"""The DVD drives media_list reports, and the disc changes mount and eject make."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.errors import BackendError
from hyperv_console_mcp.hostwmi import WmiBackend
from hyperv_console_mcp.hostwmi.connection import WmiFailure
from hyperv_console_mcp.hostwmi.media import media_eject, media_list, media_mount

VM_ID = "D5AD7D1D-5CB3-441F-A9D7-138640C8F61A"
CONTROLLER = "947A833B-A3B1-45CA-9CF3-8C2746ED4D1B"
HOST = "HOST"
CONFIG_PATH = (
    rf"\\{HOST}\root\virtualization\v2:Msvm_VirtualSystemSettingData."
    f'InstanceID="Microsoft:{VM_ID}\\\\Realized"'
)
SERVICE_PATH = rf"\\{HOST}\root\virtualization\v2:Msvm_VirtualSystemManagementService"
OLD_ISO = r"C:\HyperV\NORTH-Test\Media\NorthTestPackage.iso"
NEW_ISO = r"C:\VMs_iso\virtio-win.iso"


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


class MediaConnection(FakeConnection):
    """A host whose disc list actually changes when the provider is told to.

    The real provider answers a media change by rewriting the disc setting, and
    ``media_mount`` reads the drive back afterwards — so the fake has to apply
    the same effect or the read-back proves nothing.
    """

    def __init__(
        self,
        *,
        state: int = 3,
        discs: list[dict[str, Any]] | None = None,
        drives: tuple[int, ...] = (1, 2),
        return_code: int = 0,
    ) -> None:
        super().__init__([])
        self.state = state
        self.discs = [_disc(2, OLD_ISO)] if discs is None else discs
        self.drives = [_drive(number) for number in drives]
        self.return_code = return_code
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.job_reads: list[str] = []

    def query(self, wql: str, *, namespace: str = "") -> list[dict[str, Any]]:
        self.queries.append(wql)
        if "FROM Msvm_ComputerSystem" in wql:
            return [_computer(EnabledState=self.state)]
        if "FROM Msvm_VirtualSystemManagementService" in wql:
            return [{"__PATH": SERVICE_PATH}]
        if "System:Realized" in wql:
            return [{"__PATH": CONFIG_PATH}]
        if "FROM Msvm_StorageAllocationSettingData" in wql:
            return list(self.discs)
        if "FROM Msvm_ResourceAllocationSettingData" in wql:
            return list(self.drives)
        raise AssertionError(f"unexpected query: {wql}")

    def get(self, path: str, *, namespace: str = "") -> dict[str, Any]:
        self.job_reads.append(path)
        return {"JobState": 7}

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        self.calls.append((path, method, parameters))
        if method == "RemoveResourceSettings":
            removed = set(parameters["ResourceSettings"])
            self.discs = [row for row in self.discs if row["__PATH"] not in removed]
        elif method == "AddResourceSettings":
            self.discs.append(_disc(2, _iso_of(parameters["ResourceSettings"][0])))
        if self.return_code:
            return {"ReturnValue": self.return_code}
        return {"ReturnValue": 0}

    def methods(self) -> list[str]:
        return [method for _, method, _ in self.calls]


def _iso_of(xml: str) -> str:
    """The path inside the embedded instance's ``HostResource`` array."""
    value = xml.split("<VALUE.ARRAY><VALUE>", 1)[1].split("</VALUE>", 1)[0]
    return html.unescape(value)


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
    instance = f"Microsoft:{VM_ID}\\{CONTROLLER}\\0\\{location}\\D"
    return {
        "InstanceID": instance,
        "ResourceSubType": "Microsoft:Hyper-V:Synthetic DVD Drive",
        "Parent": "",
        "HostResource": [],
        "__PATH": (
            rf"\\{HOST}\root\virtualization\v2:Msvm_ResourceAllocationSettingData."
            f'InstanceID="{instance.replace(chr(92), chr(92) * 2)}"'
        ),
    }


def _disc(location: int, path: str | None) -> dict[str, Any]:
    instance = f"Microsoft:{VM_ID}\\{CONTROLLER}\\0\\{location}\\L"
    return {
        "InstanceID": instance,
        "ResourceSubType": "Microsoft:Hyper-V:Virtual CD/DVD Disk",
        "Parent": (
            rf"\\{HOST}\root\virtualization\v2:Msvm_ResourceAllocationSettingData.InstanceID="
            f'"Microsoft:{VM_ID}\\\\{CONTROLLER}\\\\0\\\\{location}\\\\D"'
        ),
        "HostResource": [path] if path else [],
        "__PATH": (
            rf"\\{HOST}\root\virtualization\v2:Msvm_StorageAllocationSettingData."
            f'InstanceID="{instance.replace(chr(92), chr(92) * 2)}"'
        ),
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


def test_mount_takes_the_old_disc_out_before_putting_the_new_one_in(tmp_path: Path) -> None:
    iso = tmp_path / "virtio-win.iso"
    iso.write_bytes(b"")
    connection = MediaConnection()
    result = media_mount(
        connection,
        {"vm": "NORTH-Test", "path": str(iso), "controller_number": 0, "controller_location": 2},
    )
    assert connection.methods() == ["RemoveResourceSettings", "AddResourceSettings"]
    remove = connection.calls[0][2]["ResourceSettings"]
    assert remove == [_disc(2, OLD_ISO)["__PATH"]]
    add = connection.calls[1][2]
    assert add["AffectedConfiguration"] == CONFIG_PATH
    assert result == {
        "vm": "NORTH-Test",
        "drive": {
            "controller_number": 0,
            "controller_location": 2,
            "path": str(iso),
            "mounted": True,
        },
        "boot_first": False,
        "completed": True,
    }


def test_mount_leaves_another_drives_disc_alone(tmp_path: Path) -> None:
    """The disc is found by its parent, so a second drive's media is untouched."""
    iso = tmp_path / "virtio-win.iso"
    iso.write_bytes(b"")
    other = _disc(1, r"C:\VMs_iso\Win10.iso")
    connection = MediaConnection(discs=[other, _disc(2, OLD_ISO)])
    media_mount(
        connection,
        {"vm": "NORTH-Test", "path": str(iso), "controller_number": 0, "controller_location": 2},
    )
    removed = connection.calls[0][2]["ResourceSettings"]
    assert removed == [_disc(2, OLD_ISO)["__PATH"]]
    assert other in connection.discs


def test_mount_sends_the_host_resource_as_an_array(tmp_path: Path) -> None:
    """A plain ``PROPERTY`` makes the provider read the path as a scalar."""
    iso = tmp_path / "virtio-win.iso"
    iso.write_bytes(b"")
    connection = MediaConnection()
    media_mount(
        connection,
        {"vm": "NORTH-Test", "path": str(iso), "controller_number": 0, "controller_location": 2},
    )
    xml = connection.calls[1][2]["ResourceSettings"][0]
    assert '<PROPERTY.ARRAY NAME="HostResource" TYPE="string">' in xml
    assert "<VALUE.ARRAY><VALUE>" in xml
    assert '<PROPERTY NAME="Parent" TYPE="string">' in xml


def test_the_disc_query_is_scoped_to_the_drives_slot(tmp_path: Path) -> None:
    """A LIKE on the object-path prefix matches no InstanceID, so nothing is found.

    That failure is silent: the detach removes nothing, the attach then fails
    with "the location is already in use", and the media never changes.
    """
    iso = tmp_path / "virtio-win.iso"
    iso.write_bytes(b"")
    connection = MediaConnection()
    media_mount(
        connection,
        {"vm": "NORTH-Test", "path": str(iso), "controller_number": 0, "controller_location": 2},
    )
    queries = [q for q in connection.queries if "FROM Msvm_StorageAllocationSettingData" in q]
    scoped = f"Microsoft:{VM_ID}\\\\{CONTROLLER}\\\\0\\\\2\\\\"
    assert any(scoped in query for query in queries)


def test_mount_refuses_a_running_vm(tmp_path: Path) -> None:
    iso = tmp_path / "virtio-win.iso"
    iso.write_bytes(b"")
    connection = MediaConnection(state=2)
    with pytest.raises(WmiFailure) as caught:
        media_mount(
            connection,
            {"vm": "NORTH-Test", "path": str(iso), "controller_number": 0, "controller_location": 2},
        )
    assert caught.value.code == "invalid_state"
    assert connection.calls == []


def test_mount_refuses_a_path_that_is_not_an_iso() -> None:
    with pytest.raises(WmiFailure) as caught:
        media_mount(
            MediaConnection(),
            {"vm": "NORTH-Test", "path": r"C:\VMs_iso\readme.txt", "controller_location": 2},
        )
    assert caught.value.code == "invalid_argument"


def test_mount_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(WmiFailure) as caught:
        media_mount(
            MediaConnection(),
            {"vm": "NORTH-Test", "path": str(tmp_path / "gone.iso"), "controller_location": 2},
        )
    assert caught.value.code == "not_found"


def test_mount_with_boot_first_stays_on_the_worker(tmp_path: Path) -> None:
    """``boot_first`` is firmware, not media, so the bridge serves the request."""
    iso = tmp_path / "virtio-win.iso"
    iso.write_bytes(b"")
    backend = WmiBackend(connect=lambda: MediaConnection())
    with pytest.raises(BackendError) as caught:
        backend.invoke(
            "media_mount",
            {"vm": "NORTH-Test", "path": str(iso), "controller_location": 2, "boot_first": True},
        )
    assert caught.value.code == "wmi_unsupported"


def test_a_refused_job_is_reported_with_its_code() -> None:
    with pytest.raises(WmiFailure) as caught:
        media_eject(
            MediaConnection(state=3, return_code=32775),
            {"vm": "NORTH-Test", "controller_number": 0, "controller_location": 2},
        )
    assert caught.value.code == "invalid_state"


def test_eject_removes_the_disc_and_reports_an_empty_drive() -> None:
    connection = MediaConnection()
    result = media_eject(
        connection, {"vm": "NORTH-Test", "controller_number": 0, "controller_location": 2}
    )
    assert connection.methods() == ["RemoveResourceSettings"]
    assert result == {
        "vm": "NORTH-Test",
        "drive": {
            "controller_number": 0,
            "controller_location": 2,
            "path": None,
            "mounted": False,
        },
        "completed": True,
    }


def test_eject_on_an_empty_drive_calls_nothing() -> None:
    connection = MediaConnection(discs=[])
    result = media_eject(
        connection, {"vm": "NORTH-Test", "controller_number": 0, "controller_location": 2}
    )
    assert connection.calls == []
    assert result["drive"]["mounted"] is False


def test_a_drive_that_does_not_exist_is_not_created() -> None:
    connection = MediaConnection(drives=(1,))
    with pytest.raises(WmiFailure) as caught:
        media_eject(
            connection, {"vm": "NORTH-Test", "controller_number": 0, "controller_location": 2}
        )
    assert caught.value.code == "not_found"
    assert "does not create drives" in str(caught.value)
