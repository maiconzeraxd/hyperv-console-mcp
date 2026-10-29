"""Installation media on an existing DVD drive.

A DVD drive is an ``Msvm_ResourceAllocationSettingData`` of resource type 16,
named ``<vm>\\<controller>\\<number>\\<location>\\D``; the disc inside it is a
separate ``Msvm_StorageAllocationSettingData`` of the virtual-CD subtype whose
``Parent`` points at the drive. Listing is two queries; changing the disc is a
``ModifyResourceSettings`` on the management service, which the caller reaches
only while the VM is Off — the server never stops a VM to change its media.
"""

from __future__ import annotations

from typing import Any

from .connection import Connection, WmiFailure
from .vm import VM_STATES, _int, dvd_drives, find_vm, generation

# Msvm_ComputerSystem.EnabledState for a stopped VM: the only state in which
# Hyper-V accepts a media change.
OFF = 3


def media_list(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = find_vm(connection, str(payload.get("vm") or ""))
    identifier = str(vm.get("Name") or "")
    return {
        "vm": vm.get("ElementName"),
        "state": VM_STATES.get(_int(vm.get("EnabledState")), "Other"),
        "generation": generation(connection, identifier),
        "dvd_drives": dvd_drives(connection, identifier),
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
