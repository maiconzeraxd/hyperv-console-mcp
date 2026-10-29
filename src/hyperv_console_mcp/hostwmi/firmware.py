"""The generation 2 firmware: Secure Boot, and the boot device the cmdlet names.

``Get-VMFirmware`` reads the VM's realized setting, so this does too:
``SecureBootEnabled`` is the flag the cmdlet prints as On or Off, and
``VirtualSystemSubType`` carries the generation. A generation 1 VM has neither,
and the bridge answers with the support flag instead of failing — which is what
this does, so callers see one shape for both generations.

``PreferredBootDevice`` has no counterpart in the setting: the cmdlet on this
host's module does not carry the property at all, and the bridge reports null
for it. It is null here for the same reason, and it is the one field that would
have to be revisited on a host whose module exposes it.
"""

from __future__ import annotations

from typing import Any

from .connection import Connection, WmiFailure, wql_literal
from .vm import VM_STATES, _generation, _int, find_vm

_GENERATION_2 = 2
_FIELDS = "VirtualSystemSubType, SecureBootEnabled"


def vm_firmware(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = find_vm(connection, str(payload.get("vm") or ""))
    setting = _setting(connection, str(vm.get("Name") or ""))
    generation = _generation(setting)
    supported = generation == _GENERATION_2
    return {
        "vm": vm.get("ElementName"),
        "generation": generation,
        "state": VM_STATES.get(_int(vm.get("EnabledState")), "Other"),
        "secure_boot_supported": supported,
        "secure_boot": ("On" if setting.get("SecureBootEnabled") else "Off") if supported else None,
        "preferred_boot_device": None,
    }


def _setting(connection: Connection, identifier: str) -> dict[str, Any]:
    """The realized setting, projected to the two fields firmware needs."""
    rows = connection.query(
        f"SELECT {_FIELDS} FROM Msvm_VirtualSystemSettingData "
        f"WHERE VirtualSystemIdentifier = {wql_literal(identifier)} "
        "AND VirtualSystemType = 'Microsoft:Hyper-V:System:Realized'"
    )
    if len(rows) != 1:
        raise WmiFailure(f"Expected one realized setting for {identifier}; found {len(rows)}.")
    return rows[0]
