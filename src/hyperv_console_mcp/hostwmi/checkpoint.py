"""Checkpoints: the snapshot setting data, and the service that changes them.

A checkpoint is an ``Msvm_VirtualSystemSettingData`` whose ``VirtualSystemType``
is the snapshot one. Its ``InstanceID`` is ``Microsoft:<snapshot id>`` — the id
``Get-VMSnapshot`` reports — and its ``Parent`` names the checkpoint before it,
which is the chain the cmdlet shows.

Creating one is two calls, not one. ``CreateSnapshot`` takes the name in
``SnapshotSettings``, but that parameter is a single string and the provider
ignored every embedded instance tried in it on the live host: the checkpoint
always came out with Hyper-V's default name (``<vm> - (dd/mm/yyyy - hh:mm:ss)``).
The name is therefore applied afterwards, with ``ModifySystemSettings`` on the
new setting's ``ElementName`` — which is what ``Rename-VMSnapshot`` does — and
that call does work. ``CreateSnapshot`` answers no id, so the new checkpoint is
found by comparing the list before and after.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from xml.sax.saxutils import escape

from .connection import Connection, WmiFailure, wql_literal
from .jobs import await_job
from .vm import find_vm

_SNAPSHOT = "Microsoft:Hyper-V:Snapshot:Realized"
_FIELDS = "InstanceID, ElementName, CreationTime, Notes, Parent"

# Msvm_VirtualSystemSnapshotService.CreateSnapshot's SnapshotType, whose ValueMap
# is 2, 3 and a vendor range: 2 is a full checkpoint, 3 is disk only. The bridge
# passes no type to Checkpoint-VM, and a full one is what that default makes.
_FULL = 2
# The bridge waits up to 300 s for a new checkpoint to be listed, because
# Checkpoint-VM -Passthru can return before Hyper-V publishes it.
_WAIT_SECONDS = 300.0
_POLL_SECONDS = 0.25

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


def checkpoint_create(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    """Take a checkpoint and give it the caller's name.

    The order matters: the provider's default name is applied first and the
    caller's name second, because there is no way to pass one into
    ``CreateSnapshot``. A rename that fails leaves a checkpoint under the
    default name, so the error says which id it is.
    """
    vm = find_vm(connection, str(payload.get("vm") or ""))
    identifier = str(vm.get("Name") or "")
    wanted = str(payload.get("name") or "")
    known = {str(row.get("InstanceID") or "") for row in _snapshots(connection, identifier)}
    response = connection.call(
        _service(connection),
        "CreateSnapshot",
        AffectedSystem=str(vm.get("__PATH") or ""),
        SnapshotSettings="",
        SnapshotType=_FULL,
    )
    await_job(connection, response)
    created = _wait_for_new(connection, identifier, known)
    _rename(connection, created, wanted)
    return {"checkpoint": describe(_reread(connection, created))}


def _wait_for_new(
    connection: Connection, identifier: str, known: set[str]
) -> dict[str, Any]:
    """The checkpoint the call just made, which the provider does not name."""
    deadline = time.monotonic() + _WAIT_SECONDS
    while True:
        fresh = [row for row in _snapshots(connection, identifier) if _key(row) not in known]
        if len(fresh) == 1:
            return fresh[0]
        if len(fresh) > 1:
            raise WmiFailure(
                f"Expected one new checkpoint on '{identifier}'; found {len(fresh)}."
            )
        if time.monotonic() >= deadline:
            raise WmiFailure(
                f"The checkpoint was created but is still not listed after {_WAIT_SECONDS:.0f}s."
            )
        time.sleep(_POLL_SECONDS)


def _rename(connection: Connection, snapshot: dict[str, Any], name: str) -> None:
    """``Rename-VMSnapshot`` is this one call, on the setting's ElementName."""
    response = connection.call(
        _management_service(connection),
        "ModifySystemSettings",
        SystemSettings=_rename_xml(snapshot, name),
    )
    await_job(connection, response)


def _rename_xml(snapshot: dict[str, Any], name: str) -> str:
    return (
        '<INSTANCE CLASSNAME="Msvm_VirtualSystemSettingData">'
        f'<PROPERTY NAME="InstanceID" TYPE="string"><VALUE>{escape(_key(snapshot))}</VALUE></PROPERTY>'
        f'<PROPERTY NAME="ElementName" TYPE="string"><VALUE>{escape(name)}</VALUE></PROPERTY>'
        "</INSTANCE>"
    )


def _reread(connection: Connection, snapshot: dict[str, Any]) -> dict[str, Any]:
    """The setting as it is now — the rename may not have reached this copy."""
    path = str(snapshot.get("__PATH") or "")
    return connection.get(path) if path else snapshot


def _key(row: dict[str, Any]) -> str:
    return str(row.get("InstanceID") or "")


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


def _management_service(connection: Connection) -> str:
    """Renaming a setting is the management service's call, not the snapshot one."""
    rows = connection.query("SELECT * FROM Msvm_VirtualSystemManagementService")
    if len(rows) != 1:
        raise WmiFailure(
            f"Expected one management service on this host; found {len(rows)}.",
            code="wmi_unavailable",
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
