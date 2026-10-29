"""Installation media on an existing DVD drive.

A DVD drive is an ``Msvm_ResourceAllocationSettingData`` of resource type 16,
named ``<vm>\\<controller>\\<number>\\<location>\\D``; the disc inside it is a
separate ``Msvm_StorageAllocationSettingData`` of the virtual-CD subtype whose
``Parent`` names the drive. Listing is two queries.

Changing the disc is not a ``ModifyResourceSettings``: the provider accepts that
call and changes nothing, and when it is given the new path it fails the job. It
is a detach and an attach — ``RemoveResourceSettings`` with the disc's object
path, then ``AddResourceSettings`` with a new disc setting for the same drive —
which is what the live host accepted. The disc setting travels as an embedded
instance in WMI's DTD 2.0 text, where an array property is a
``PROPERTY.ARRAY``/``VALUE.ARRAY`` element; without that wrapper the provider
reads the path as a scalar and the job fails.

Boot order is the one part left on the worker: ``boot_first`` changes firmware,
not media, and the caller gets ``wmi_unsupported`` so the bridge serves the whole
request.
"""

from __future__ import annotations

import os
from typing import Any
from xml.sax.saxutils import escape

from .connection import Connection, WmiFailure
from .jobs import await_job
from .vm import VM_STATES, _int, _refers_to, dvd_drives, find_vm, generation

# Msvm_ComputerSystem.EnabledState for a stopped VM: the only state in which
# Hyper-V accepts a media change.
OFF = 3
_ISO_SUBTYPE = "Microsoft:Hyper-V:Virtual CD/DVD Disk"
_ISO_RESOURCE_TYPE = 31
_DVD_DRIVE = 16


def media_list(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = find_vm(connection, str(payload.get("vm") or ""))
    identifier = str(vm.get("Name") or "")
    return {
        "vm": vm.get("ElementName"),
        "state": VM_STATES.get(_int(vm.get("EnabledState")), "Other"),
        "generation": generation(connection, identifier),
        "dvd_drives": dvd_drives(connection, identifier),
    }


def media_mount(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = _off(connection, payload, "changing installation media or boot order")
    if payload.get("boot_first"):
        raise WmiFailure(
            "boot_first changes the firmware boot order, which the PowerShell worker owns.",
            code="wmi_unsupported",
        )
    iso = _iso_path(payload.get("path"))
    number = _int(payload.get("controller_number"))
    location = _int(payload.get("controller_location"))
    identifier = str(vm.get("Name") or "")
    drive = _drive_at(connection, identifier, number, location)
    _detach(connection, drive)
    response = connection.call(
        _service(connection),
        "AddResourceSettings",
        AffectedConfiguration=_configuration(connection, identifier),
        ResourceSettings=[_disc_xml(drive, iso)],
    )
    await_job(connection, response)
    return {
        "vm": vm.get("ElementName"),
        "drive": _drive_state(connection, identifier, number, location),
        "boot_first": False,
        "completed": True,
    }


def media_eject(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = _off(connection, payload, "ejecting installation media")
    number = _int(payload.get("controller_number"))
    location = _int(payload.get("controller_location"))
    identifier = str(vm.get("Name") or "")
    drive = _drive_at(connection, identifier, number, location)
    _detach(connection, drive)
    return {
        "vm": vm.get("ElementName"),
        "drive": _drive_state(connection, identifier, number, location),
        "completed": True,
    }


def _off(connection: Connection, payload: dict[str, Any], reason: str) -> dict[str, Any]:
    """The VM, refused unless it is stopped — a media change needs it Off."""
    vm = find_vm(connection, str(payload.get("vm") or ""))
    if _int(vm.get("EnabledState")) != OFF:
        raise WmiFailure(
            f"The VM must be Off before {reason}. The server will not stop it automatically.",
            code="invalid_state",
        )
    return vm


def _iso_path(value: Any) -> str:
    """The bridge's own check: an existing file whose extension is ``.iso``."""
    path = str(value or "")
    if os.path.splitext(path)[1].lower() != ".iso":
        raise WmiFailure("The media path must be an existing .iso file.", code="invalid_argument")
    if not os.path.isfile(path):
        raise WmiFailure("The media path must be an existing .iso file.", code="not_found")
    return path


def _drive_at(
    connection: Connection, identifier: str, number: int, location: int
) -> dict[str, Any]:
    """The drive in one slot, by the numbers rather than by a display name."""
    rows = connection.query(
        "SELECT InstanceID, ResourceSubType, Parent, HostResource "
        f"FROM Msvm_ResourceAllocationSettingData WHERE InstanceID LIKE "
        f"'{_literal('Microsoft:' + identifier)}%' AND ResourceType = {_DVD_DRIVE}"
    )
    matches = [row for row in rows if _slot(row, 2) == number and _slot(row, 3) == location]
    if len(matches) != 1:
        raise WmiFailure(
            f"Expected one DVD drive at controller {number} location {location}; "
            f"found {len(matches)}. The server does not create drives implicitly.",
            code="not_found",
        )
    return matches[0]


def _drive_state(
    connection: Connection, identifier: str, number: int, location: int
) -> dict[str, Any]:
    for drive in dvd_drives(connection, identifier):
        if drive["controller_number"] == number and drive["controller_location"] == location:
            return drive
    raise WmiFailure(f"The DVD drive at {number}/{location} disappeared from '{identifier}'.")


def _detach(connection: Connection, drive: dict[str, Any]) -> None:
    """Take the disc out, if the drive holds one.

    The disc setting is a child of the drive, which is why the match is by
    ``Parent`` and not by slot: a checkpoint carries its own copy of the same
    setting, and removing that one would change the checkpoint instead.
    """
    for row in _discs(connection, drive):
        response = connection.call(
            _service(connection), "RemoveResourceSettings", ResourceSettings=[row["__PATH"]]
        )
        await_job(connection, response)


def _discs(connection: Connection, drive: dict[str, Any]) -> list[dict[str, Any]]:
    """The disc settings of one drive.

    The LIKE is on the InstanceID prefix of the drive's own slot — the object
    path shares no prefix with it, and a LIKE on that matches nothing at all,
    which reads as "the drive is empty" and leaves the old disc in place.
    """
    prefix = str(drive.get("InstanceID") or "").rsplit("\\", 1)[0] + "\\"
    rows = connection.query(
        "SELECT InstanceID, ResourceSubType, Parent, HostResource "
        f"FROM Msvm_StorageAllocationSettingData WHERE InstanceID LIKE '{_literal(prefix)}%'"
    )
    return [
        row
        for row in rows
        if row.get("ResourceSubType") == _ISO_SUBTYPE and _refers_to(row, drive)
    ]


def _disc_xml(drive: dict[str, Any], path: str) -> str:
    """A new disc setting for this drive, as an embedded instance.

    ``HostResource`` is an array property, so it is written as
    ``PROPERTY.ARRAY``; with a plain ``PROPERTY`` the provider parses the path as
    a scalar and the job fails. Everything the provider fills in itself —
    caption, allocation units, limits — is left out on purpose: those values are
    localized text on a pt-BR host.
    """
    return (
        '<INSTANCE CLASSNAME="Msvm_StorageAllocationSettingData">'
        f'<PROPERTY NAME="ResourceType" TYPE="uint16"><VALUE>{_ISO_RESOURCE_TYPE}</VALUE></PROPERTY>'
        f'<PROPERTY NAME="ResourceSubType" TYPE="string"><VALUE>{_ISO_SUBTYPE}</VALUE></PROPERTY>'
        f'<PROPERTY NAME="Parent" TYPE="string"><VALUE>{escape(_parent(drive))}</VALUE></PROPERTY>'
        '<PROPERTY.ARRAY NAME="HostResource" TYPE="string"><VALUE.ARRAY>'
        f"<VALUE>{escape(path)}</VALUE>"
        "</VALUE.ARRAY></PROPERTY.ARRAY>"
        "</INSTANCE>"
    )


def _parent(drive: dict[str, Any]) -> str:
    """The drive as a reference: the object path with its key's backslashes doubled."""
    instance = str(drive.get("InstanceID") or "").replace("\\", "\\\\")
    return (
        f"{_prefix(drive)}:Msvm_ResourceAllocationSettingData."
        f'InstanceID="{instance}"'
    )


def _prefix(row: dict[str, Any]) -> str:
    """``\\\\HOST\\root\\virtualization\\v2``, taken from an object path."""
    path = str(row.get("__PATH") or "")
    prefix, separator, _ = path.partition(":Msvm_")
    if not separator:
        raise WmiFailure(f"Could not read the object path '{path}'.")
    return prefix


def _configuration(connection: Connection, identifier: str) -> str:
    """The VM's realized setting — the configuration a new device is added to."""
    rows = connection.query(
        "SELECT InstanceID FROM Msvm_VirtualSystemSettingData "
        f"WHERE VirtualSystemIdentifier = '{_literal(identifier)}' "
        "AND VirtualSystemType = 'Microsoft:Hyper-V:System:Realized'"
    )
    if len(rows) != 1:
        raise WmiFailure(f"Expected one realized setting for {identifier}; found {len(rows)}.")
    return str(rows[0]["__PATH"])


def _service(connection: Connection) -> str:
    rows = connection.query("SELECT * FROM Msvm_VirtualSystemManagementService")
    if len(rows) != 1:
        raise WmiFailure(
            f"Expected one management service on this host; found {len(rows)}.",
            code="wmi_unavailable",
        )
    return str(rows[0]["__PATH"])


def _slot(row: dict[str, Any], index: int) -> int:
    parts = str(row.get("InstanceID") or "").split("\\")
    try:
        return int(parts[index])
    except (IndexError, ValueError):
        return -1


def _literal(value: str) -> str:
    """A WQL literal, kept local so the LIKE pattern stays readable above."""
    return value.replace("\\", "\\\\").replace("'", "''")
