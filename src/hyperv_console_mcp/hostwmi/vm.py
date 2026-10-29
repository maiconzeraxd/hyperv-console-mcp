"""The VM object ``Get-VM`` returns, assembled from CIM instead of cmdlets.

Every field maps one-to-one onto what the bridge already answered, so callers
see the same shape. Two rules keep that honest on a localized host: nothing is
matched by a display name (``Caption`` says "Máquina Virtual" on a pt-BR host,
``ElementName`` says "Disco Rígido"), and every enum is translated from its
numeric value.

Every query names the properties it needs. The provider charges per property
read, so a ``SELECT *`` on the setting classes costs an order of magnitude more
than the four fields that end up in the answer.
"""

from __future__ import annotations

import re
from typing import Any

from .connection import Connection, WmiFailure, wql_literal

# A VM instance is the one whose Name is its own id; the host instance is
# named after the computer, which is why this and not the localized Caption.
_GUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")

# Msvm_ComputerSystem.EnabledState carries the Hyper-V VM state, not the CIM
# one: 2 is a running VM and 3 a stopped one, as Get-VM reports them.
VM_STATES = {
    1: "Other", 2: "Running", 3: "Off", 4: "Stopping", 6: "Saved", 9: "Paused",
    10: "Starting", 11: "Snapshotting", 12: "Saving", 13: "Draining", 15: "Resetting",
    16: "Pausing", 17: "Resuming", 18: "FastSaved", 19: "FastSaving",
    20: "ForceShutdown", 21: "ForceReboot", 22: "Hibernated",
    23: "ComponentServicing", 24: "Orphaned",
}

CHECKPOINT_TYPES = {2: "Disabled", 3: "Production", 4: "ProductionOnly", 5: "Standard"}
START_ACTIONS = {2: "Nothing", 3: "StartIfRunning", 4: "Start"}
STOP_ACTIONS = {2: "TurnOff", 3: "Save", 4: "ShutDown"}

_MIB = 2**20
_DVD_DRIVE = 16
_VHD_SUBTYPE = "Microsoft:Hyper-V:Virtual Hard Disk"
_ISO_SUBTYPE = "Microsoft:Hyper-V:Virtual CD/DVD Disk"
_REALIZED = "Microsoft:Hyper-V:System:Realized"
_SNAPSHOT = "Microsoft:Hyper-V:Snapshot:Realized"
_SWITCH_NAME = re.compile(r'Msvm_VirtualEthernetSwitch\..*Name="([^"]+)"')

_COMPUTER_FIELDS = "ElementName, Name, EnabledState, StatusDescriptions, OnTimeInMilliseconds"
_SETTING_FIELDS = (
    "ElementName, Version, VirtualSystemSubType, UserSnapshotType, AutomaticSnapshotsEnabled, "
    "AutomaticStartupAction, AutomaticShutdownAction"
)


def vm_list(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    computers = _computers(connection)
    vms = [describe(connection, row) for row in computers]
    return {"vms": vms, "count": len(vms)}


def vm_get(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    return {"vm": describe(connection, find_vm(connection, str(payload.get("vm") or "")))}


def find_vm(connection: Connection, name: str) -> dict[str, Any]:
    """One VM instance by the name its owner gave it."""
    matches = [row for row in _computers(connection) if row.get("ElementName") == name]
    if len(matches) != 1:
        raise WmiFailure(
            f"Expected exactly one VM named '{name}'; found {len(matches)}.", code="not_found"
        )
    return matches[0]


def _computers(connection: Connection) -> list[dict[str, Any]]:
    """The VM instances, refusing an empty answer that is really a refusal.

    Without Hyper-V Administrator rights the provider hides the VMs instead of
    failing, so an empty list would read as "this host has no VMs". Asking for
    the management service tells the two apart: it is filtered the same way.
    """
    rows = [
        row
        for row in connection.query(f"SELECT {_COMPUTER_FIELDS} FROM Msvm_ComputerSystem")
        if _GUID.fullmatch(str(row.get("Name") or ""))
    ]
    if rows:
        return rows
    if not connection.query("SELECT * FROM Msvm_VirtualSystemManagementService"):
        raise WmiFailure(
            "The Hyper-V WMI provider hides the virtual machines from this account. "
            "Run the server elevated or grant Hyper-V Administrator rights.",
            code="wmi_unavailable",
        )
    return rows


def describe(connection: Connection, computer: dict[str, Any]) -> dict[str, Any]:
    """The Get-VM shape for one computer instance."""
    identifier = str(computer.get("Name") or "")
    settings = _settings(connection, identifier)
    components = _components(connection, identifier)
    memory = components["memory"][0] if components["memory"] else None
    processor = components["processor"][0] if components["processor"] else None
    enabled = _int(computer.get("EnabledState"))
    startup = _memory_bytes(memory)
    return {
        "id": identifier.lower(),
        "name": computer.get("ElementName"),
        "state": VM_STATES.get(enabled, "Other"),
        "state_code": enabled,
        "status": _first_description(computer),
        "generation": _generation(settings),
        "version": str(settings.get("Version") or ""),
        "processor_count": _int(processor.get("VirtualQuantity")) if processor else 0,
        "memory_startup_bytes": startup,
        # Dynamic memory would make these differ; the runtime allocation is not
        # a CIM property, so the startup figure is the honest answer here.
        "memory_assigned_bytes": startup,
        "uptime_seconds": _uptime(computer),
        "checkpoint_type": CHECKPOINT_TYPES.get(_int(settings.get("UserSnapshotType")), "Unknown"),
        "automatic_checkpoints_enabled": bool(settings.get("AutomaticSnapshotsEnabled")),
        "automatic_start_action": START_ACTIONS.get(
            _int(settings.get("AutomaticStartupAction")), "Unknown"
        ),
        "automatic_stop_action": STOP_ACTIONS.get(
            _int(settings.get("AutomaticShutdownAction")), "Unknown"
        ),
        "checkpoint_count": _checkpoints(connection, identifier),
        "hard_disks": [
            _disk(row)
            for row in components["storage"]
            if row.get("ResourceSubType") == _VHD_SUBTYPE
        ],
        "dvd_drives": [_drive(row, components["storage"]) for row in components["drives"]],
        "network_adapters": _adapters(connection, components),
    }


def _settings(connection: Connection, identifier: str) -> dict[str, Any]:
    rows = connection.query(
        f"SELECT {_SETTING_FIELDS} FROM Msvm_VirtualSystemSettingData "
        f"WHERE VirtualSystemIdentifier = {wql_literal(identifier)} "
        f"AND VirtualSystemType = {wql_literal(_REALIZED)}"
    )
    if len(rows) != 1:
        raise WmiFailure(f"Expected one realized setting for {identifier}; found {len(rows)}.")
    return rows[0]


def _queries(identifier: str) -> dict[str, str]:
    """One projected query per resource class.

    The association ``Msvm_VirtualSystemSettingDataComponent`` answers the same
    set in one call, but it returns every property of every object, which is
    slower than these narrow queries.
    """
    prefix = f"WHERE InstanceID LIKE {wql_literal('Microsoft:' + identifier + '%')}"
    return {
        "memory": (
            "SELECT InstanceID, VirtualQuantity, VirtualQuantityUnits "
            f"FROM Msvm_MemorySettingData {prefix}"
        ),
        "processor": f"SELECT InstanceID, VirtualQuantity FROM Msvm_ProcessorSettingData {prefix}",
        "storage": (
            "SELECT InstanceID, ResourceSubType, Parent, HostResource "
            f"FROM Msvm_StorageAllocationSettingData {prefix}"
        ),
        "drives": (
            "SELECT InstanceID, ResourceSubType, Parent, HostResource "
            f"FROM Msvm_ResourceAllocationSettingData {prefix} AND ResourceType = {_DVD_DRIVE}"
        ),
        "ports": (
            "SELECT InstanceID, ElementName, Address "
            f"FROM Msvm_SyntheticEthernetPortSettingData {prefix}"
        ),
        "allocations": (
            f"SELECT InstanceID, Parent, HostResource FROM Msvm_EthernetPortAllocationSettingData {prefix}"
        ),
    }


def _components(connection: Connection, identifier: str) -> dict[str, list[dict[str, Any]]]:
    return {name: connection.query(wql) for name, wql in _queries(identifier).items()}


def generation(connection: Connection, identifier: str) -> int:
    """The VM generation, from the subtype of its realized setting."""
    return _generation(_settings(connection, identifier))


def dvd_drives(connection: Connection, identifier: str) -> list[dict[str, Any]]:
    """The DVD drives of one VM, saying whether each one holds media.

    ``Get-VM`` reports only the path; ``Get-VMDvdDrive`` — what ``media_list``
    shows — adds the flag, so it is added here instead of in the shared mapping.
    """
    queries = _queries(identifier)
    storage = connection.query(queries["storage"])
    drives = []
    for row in connection.query(queries["drives"]):
        drive = _drive(row, storage)
        drive["mounted"] = bool(str(drive["path"] or "").strip())
        drives.append(drive)
    return drives


def _checkpoints(connection: Connection, identifier: str) -> int:
    return len(
        connection.query(
            "SELECT InstanceID FROM Msvm_VirtualSystemSettingData "
            f"WHERE VirtualSystemType = {wql_literal(_SNAPSHOT)} "
            f"AND VirtualSystemIdentifier = {wql_literal(identifier)}"
        )
    )


def _adapters(
    connection: Connection, components: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    ports = components["ports"]
    if not ports:
        return []
    switches = {
        str(row.get("Name")): row.get("ElementName")
        for row in connection.query("SELECT Name, ElementName FROM Msvm_VirtualEthernetSwitch")
    }
    # The port setting carries no status; the runtime port does, and Get-VM
    # reports the runtime OperationalStatus as the adapter's Status.
    statuses = {
        str(row.get("DeviceID") or "").rsplit(":", 1)[-1]: list(row.get("OperationalStatus") or [])
        for row in connection.query(
            "SELECT DeviceID, OperationalStatus FROM Msvm_SyntheticEthernetPort"
        )
    }
    addresses: dict[str, list[str]] = {}
    for row in connection.query(
        "SELECT InstanceID, IPAddresses FROM Msvm_GuestNetworkAdapterConfiguration"
    ):
        suffix = str(row.get("InstanceID") or "").split("\\")[-1]
        addresses[suffix] = [str(value) for value in row.get("IPAddresses") or []]
    result = []
    for port in ports:
        suffix = str(port.get("InstanceID") or "").split("\\")[-1]
        result.append(
            {
                "name": port.get("ElementName"),
                "switch_name": _switch_name(port, components["allocations"], switches),
                "status": statuses.get(suffix, []),
                "mac_address": port.get("Address"),
                "ip_addresses": addresses.get(suffix, []),
            }
        )
    return result


def _switch_name(
    port: dict[str, Any], allocations: list[dict[str, Any]], switches: dict[str, Any]
) -> str | None:
    """The switch an adapter is attached to, by the name its owner gave it."""
    for row in allocations:
        if not _refers_to(row, port):
            continue
        for resource in row.get("HostResource") or []:
            match = _SWITCH_NAME.search(str(resource))
            if match:
                return switches.get(match.group(1))
    return None


def _disk(component: dict[str, Any]) -> dict[str, Any]:
    """``<vm>\\<controller>\\<number>\\<location>\\L`` names the disk's slot."""
    parts = str(component.get("InstanceID") or "").split("\\")
    resources = component.get("HostResource") or []
    return {
        "controller_number": _slot(parts, 2),
        "controller_location": _slot(parts, 3),
        "path": str(resources[0]) if resources else None,
    }


def _drive(component: dict[str, Any], storage: list[dict[str, Any]]) -> dict[str, Any]:
    parts = str(component.get("InstanceID") or "").split("\\")
    media = None
    for candidate in storage:
        if candidate.get("ResourceSubType") != _ISO_SUBTYPE or not _refers_to(candidate, component):
            continue
        resources = candidate.get("HostResource") or []
        media = str(resources[0]) if resources else None
        break
    return {
        "controller_number": _slot(parts, 2),
        "controller_location": _slot(parts, 3),
        "path": media,
    }


def _refers_to(child: dict[str, Any], parent: dict[str, Any]) -> bool:
    """Whether a child setting's ``Parent`` is this parent's object path.

    An embedded object path doubles every backslash, so the InstanceID has to
    be escaped before it can be looked for in the reference.
    """
    instance = str(parent.get("InstanceID") or "")
    reference = str(child.get("Parent") or "")
    return bool(instance) and instance.replace("\\", "\\\\") in reference


def _slot(parts: list[str], index: int) -> int:
    try:
        return int(parts[index])
    except (IndexError, ValueError):
        return 0


def _memory_bytes(component: dict[str, Any] | None) -> int:
    if not component:
        return 0
    value = _int(component.get("VirtualQuantity"))
    units = str(component.get("VirtualQuantityUnits") or "")
    return value * _MIB if units == "byte * 2^20" else value


def _generation(settings: dict[str, Any]) -> int:
    subtype = str(settings.get("VirtualSystemSubType") or "")
    return _int(subtype.rsplit(":", 1)[-1])


def _uptime(computer: dict[str, Any]) -> float:
    """Milliseconds since the VM started, as the bridge rounds seconds."""
    return round(_int(computer.get("OnTimeInMilliseconds")) / 1000, 3)


def _first_description(computer: dict[str, Any]) -> str | None:
    """``StatusDescriptions`` is the string ``Get-VM`` shows as ``Status``."""
    for value in computer.get("StatusDescriptions") or []:
        return str(value)
    return None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
