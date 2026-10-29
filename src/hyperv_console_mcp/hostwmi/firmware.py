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
from xml.sax.saxutils import escape

from .connection import Connection, WmiFailure, wql_literal
from .jobs import await_job
from .vm import VM_STATES, _generation, _int, find_vm

_GENERATION_2 = 2
_OFF = 3
_FIELDS = "InstanceID, VirtualSystemSubType, SecureBootEnabled"


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


def vm_secure_boot(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    """Turn Secure Boot on or off, on a stopped generation 2 VM.

    The setting travels as a partial embedded instance keyed by the realized
    setting's InstanceID — the same ``ModifySystemSettings`` shape ``Set-VM``
    uses for memory and processor counts. The answer is read back rather than
    echoed, and ``enabled`` keeps what was asked, as the bridge reports both.
    """
    vm = find_vm(connection, str(payload.get("vm") or ""))
    setting = _setting(connection, str(vm.get("Name") or ""))
    if _generation(setting) != _GENERATION_2:
        raise WmiFailure("Secure Boot applies only to generation 2 VMs.", code="invalid_argument")
    if _int(vm.get("EnabledState")) != _OFF:
        raise WmiFailure(
            "The VM must be Off before Secure Boot can be changed. The server will not stop it.",
            code="invalid_state",
        )
    enable = bool(payload.get("enable"))
    response = connection.call(
        _service(connection), "ModifySystemSettings", SystemSettings=_secure_boot_xml(setting, enable)
    )
    await_job(connection, response)
    after = _setting(connection, str(vm.get("Name") or ""))
    return {
        "vm": vm.get("ElementName"),
        "secure_boot": "On" if after.get("SecureBootEnabled") else "Off",
        "enabled": enable,
    }


def _secure_boot_xml(setting: dict[str, Any], enable: bool) -> str:
    return (
        '<INSTANCE CLASSNAME="Msvm_VirtualSystemSettingData">'
        f'<PROPERTY NAME="InstanceID" TYPE="string"><VALUE>{escape(str(setting.get("InstanceID") or ""))}</VALUE></PROPERTY>'
        f'<PROPERTY NAME="SecureBootEnabled" TYPE="boolean"><VALUE>{"TRUE" if enable else "FALSE"}</VALUE></PROPERTY>'
        "</INSTANCE>"
    )


def _service(connection: Connection) -> str:
    rows = connection.query("SELECT * FROM Msvm_VirtualSystemManagementService")
    if len(rows) != 1:
        raise WmiFailure(
            f"Expected one management service on this host; found {len(rows)}.",
            code="wmi_unavailable",
        )
    return str(rows[0]["__PATH"])


def _setting(connection: Connection, identifier: str) -> dict[str, Any]:
    """The realized setting, projected to the fields firmware needs."""
    rows = connection.query(
        f"SELECT {_FIELDS} FROM Msvm_VirtualSystemSettingData "
        f"WHERE VirtualSystemIdentifier = {wql_literal(identifier)} "
        "AND VirtualSystemType = 'Microsoft:Hyper-V:System:Realized'"
    )
    if len(rows) != 1:
        raise WmiFailure(f"Expected one realized setting for {identifier}; found {len(rows)}.")
    return rows[0]
