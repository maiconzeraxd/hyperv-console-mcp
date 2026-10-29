"""Checkpoints: the snapshot setting data, and the service that changes them.

A checkpoint is an ``Msvm_VirtualSystemSettingData`` whose ``VirtualSystemType``
is the snapshot one. Its ``InstanceID`` is ``Microsoft:<snapshot id>`` — the id
``Get-VMSnapshot`` reports — and its ``Parent`` names the checkpoint before it,
which is the chain the cmdlet shows.

Creating one stays on the bridge for now. The provider takes the checkpoint's
name in ``SnapshotSettings``, an embedded-instance string, and ignored every
encoding tried on the live host: CIM XML with and without the prolog and the key
property, the instance object itself, and the MOF of a cloned setting. A
checkpoint made from Python therefore always carries the provider's default
name, which is not what the tool promises. Listing, applying and destroying need
no name and are served from here.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .connection import Connection, WmiFailure, wql_literal
from .jobs import await_job
from .vm import find_vm

_SNAPSHOT = "Microsoft:Hyper-V:Snapshot:Realized"
_FIELDS = "InstanceID, ElementName, CreationTime, Notes, Parent"

# A CIM DATETIME: 20261029014417.778334-000, where the trailing group is the
# offset in minutes from UTC and "-000" means "UTC, offset unknown".
_CIM_DATETIME = re.compile(
    r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\.(\d{6})([+-])(\d{3})"
)
_GUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")


def checkpoint_list(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = find_vm(connection, str(payload.get("vm") or ""))
    checkpoints = [
        describe(row) for row in _snapshots(connection, str(vm.get("Name") or ""))
    ]
    return {"vm": vm.get("ElementName"), "checkpoints": checkpoints, "count": len(checkpoints)}


def checkpoint_restore(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    snapshot = _target(connection, payload)
    response = connection.call(_service(connection), "ApplySnapshot", Snapshot=snapshot["__PATH"])
    await_job(connection, response)
    return {"restored": True, "checkpoint": describe(snapshot)}


def checkpoint_delete(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    snapshot = _target(connection, payload)
    response = connection.call(
        _service(connection), "DestroySnapshot", AffectedSnapshot=snapshot["__PATH"]
    )
    await_job(connection, response)
    return {"deleted": True, "checkpoint": describe(snapshot)}


def describe(row: dict[str, Any]) -> dict[str, Any]:
    """The Get-VMSnapshot shape for one snapshot setting."""
    identifier = str(row.get("InstanceID") or "")
    return {
        "id": identifier.rsplit(":", 1)[-1].lower(),
        "name": row.get("ElementName"),
        "creation_time": _local_time(row.get("CreationTime")),
        "parent_snapshot_id": _parent_id(row.get("Parent")),
        "notes": _notes(row.get("Notes")),
    }


def _target(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = find_vm(connection, str(payload.get("vm") or ""))
    return _find(connection, vm, str(payload.get("snapshot_id") or ""))


def _find(connection: Connection, vm: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    wanted = snapshot_id.lower()
    matches = [
        row
        for row in _snapshots(connection, str(vm.get("Name") or ""))
        if str(row.get("InstanceID") or "").rsplit(":", 1)[-1].lower() == wanted
    ]
    if len(matches) != 1:
        raise WmiFailure(
            f"Expected checkpoint '{snapshot_id}' on '{vm.get('ElementName')}'; found {len(matches)}.",
            code="not_found",
        )
    return matches[0]


def _snapshots(connection: Connection, identifier: str) -> list[dict[str, Any]]:
    """The VM's checkpoints, oldest first — the order Get-VMSnapshot shows."""
    rows = connection.query(
        f"SELECT {_FIELDS} FROM Msvm_VirtualSystemSettingData "
        f"WHERE VirtualSystemType = {wql_literal(_SNAPSHOT)} "
        f"AND VirtualSystemIdentifier = {wql_literal(identifier)}"
    )
    return sorted(
        rows,
        key=lambda row: (str(row.get("CreationTime") or ""), str(row.get("ElementName") or "")),
    )


def _service(connection: Connection) -> str:
    rows = connection.query("SELECT * FROM Msvm_VirtualSystemSnapshotService")
    if len(rows) != 1:
        raise WmiFailure(
            f"Expected one snapshot service on this host; found {len(rows)}.", code="wmi_unavailable"
        )
    return str(rows[0]["__PATH"])


def _parent_id(reference: Any) -> str | None:
    """The checkpoint before this one, from the reference WMI keeps in ``Parent``."""
    found = _GUID.findall(str(reference or ""))
    return found[-1].lower() if found else None


def _notes(value: Any) -> str | None:
    """``Notes`` is a string to Get-VMSnapshot but an array to WMI."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return "\n".join(str(item) for item in value)


def _local_time(value: Any) -> str | None:
    """A CIM DATETIME as the ISO string the bridge prints, in the host's zone.

    ``Get-VMSnapshot`` hands the cmdlet a local ``DateTime`` and the bridge
    writes it with round-trip precision — seven fractional digits and an
    offset — so the same instant has to come out the same way here.
    """
    match = _CIM_DATETIME.fullmatch(str(value or ""))
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(match.group(index)) for index in range(1, 7))
    offset = int(match.group(9)) * (-1 if match.group(8) == "-" else 1)
    moment = datetime(
        year, month, day, hour, minute, second, int(match.group(7)),
        tzinfo=timezone(timedelta(minutes=offset)),
    )
    local = moment.astimezone()
    zone = local.strftime("%z")
    return f"{local.strftime('%Y-%m-%dT%H:%M:%S')}.{local.microsecond:06d}0{zone[:3]}:{zone[3:]}"
