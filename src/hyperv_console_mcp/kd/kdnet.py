"""KDNET: host-side setup for network kernel debugging on the internal lab switch.

The key is generated on the host and kept only in the Windows Credential
Manager. It is written into the guest's BCD through PowerShell Direct and into
kd.exe's command line when a session starts; it never appears in a tool result,
an event, or the audit log. Every host change is confined to the dedicated
internal switch, one adapter of the target VM, one firewall rule scoped to
kd.exe on one UDP port, and files under the configured KD root.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from ..credentials import KDNET_TARGET_PREFIX, SecretStore
from ..errors import BackendError, ValidationError
from ..transport.psdirect import PowerShellDirectTransport
from ..transport.ssh import ps_quote
from ..transport.ssh_setup import SWITCH_NAME, host_address
from .discovery import find_debugger
from .paths import kdnet_state_path, read_json, write_json

PORT_MIN = 50_000
PORT_MAX = 50_039
FIREWALL_RULE_PREFIX = "HyperVConsoleMCP KDNET "
OK_MARKER = "HVMCP_KDNET_OK:"
ERROR_MARKER = "HVMCP_KDNET_ERROR:"


def generate_key() -> str:
    """Four dot-separated groups in 1..255: the form bcdedit and kd both accept."""
    return ".".join(str(secrets.randbelow(255) + 1) for _ in range(4))


def firewall_rule_name(vm_name: str) -> str:
    return FIREWALL_RULE_PREFIX + vm_name


def key_store() -> SecretStore:
    return SecretStore(KDNET_TARGET_PREFIX)


def read_state(settings: Any, vm_name: str) -> dict[str, Any] | None:
    return read_json(kdnet_state_path(settings, vm_name))


def choose_port(
    settings: Any, vm_name: str, *, requested: int | None = None
) -> int:
    """Lowest port in the KDNET range that no other prepared VM claims."""
    if requested is not None:
        if isinstance(requested, bool) or not PORT_MIN <= requested <= PORT_MAX:
            raise ValidationError(f"port must be between {PORT_MIN} and {PORT_MAX}.")
    claimed: dict[int, str] = {}
    root = kdnet_state_path(settings, vm_name).parent.parent
    if root.is_dir():
        for child in root.glob("*/kdnet.json"):
            state = read_json(child)
            if not state or not state.get("port"):
                continue
            try:
                claimed[int(state["port"])] = str(state.get("vm") or child.parent.name)
            except (TypeError, ValueError):
                continue
    if requested is not None:
        owner = claimed.get(requested)
        if owner is not None and owner.casefold() != vm_name.casefold():
            raise ValidationError(
                f"UDP port {requested} is already claimed by {owner}; pick another one."
            )
        return requested
    for candidate in range(PORT_MIN, PORT_MAX + 1):
        if candidate not in claimed:
            return candidate
    raise BackendError(
        f"No free KDNET port between {PORT_MIN} and {PORT_MAX}.", code="kdnet_no_port"
    )


def guest_command(host_ip: str, port: int, key: str) -> str:
    """Set the guest's BCD over PowerShell Direct, and report the settings masked.

    ``bcdedit /dbgsettings`` prints the key, so the command masks it before any
    of the output can travel back to the host, a tool result, or a log.
    """
    dbgsettings = (
        "bcdedit.exe /dbgsettings net "
        f"hostip:{host_ip} port:{int(port)} key:{key}"
    )
    return (
        "$ErrorActionPreference='Stop'; "
        f"$out = & {dbgsettings} 2>&1; "
        "if ($LASTEXITCODE -ne 0) { "
        f"Write-Output ({ps_quote(ERROR_MARKER)} + ($out -join ' ')); exit 1 }}; "
        "& bcdedit.exe /set '{current}' debug on 2>&1 | Out-Null; "
        "if ($LASTEXITCODE -ne 0) { "
        f"Write-Output ({ps_quote(ERROR_MARKER)} + 'could not enable debug'); exit 1 }}; "
        "$s = ((& bcdedit.exe /dbgsettings 2>&1) -join \"`n\"); "
        "$s = $s -replace 'key=\\S+', 'key ***'; "
        "$s = $s -replace '(?m)^key\\s+\\S+', 'key ***'; "
        f"Write-Output ({ps_quote(OK_MARKER)} + ($s -replace \"`r?`n\", ' | '))"
    )


def removal_command() -> str:
    return (
        "$ErrorActionPreference='Continue'; "
        "& bcdedit.exe /debug off 2>&1 | Out-Null; "
        "& bcdedit.exe /deletevalue '{current}' dbgsettings 2>&1 | Out-Null; "
        "$d = (& bcdedit.exe /enum '{current}' 2>&1) -join \"`n\"; "
        "if ($d -match 'debug\\s+Yes') { "
        f"Write-Output ({ps_quote(ERROR_MARKER)} + 'debug is still enabled'); exit 1 }}; "
        f"Write-Output ({ps_quote(OK_MARKER)} + 'debug off')"
    )


def _marker(output: str, marker: str) -> str | None:
    for line in output.splitlines():
        if line.startswith(marker):
            return line[len(marker):].strip()
    return None


def prepare(
    service: Any,
    vm: str,
    *,
    use_existing_adapter: bool = False,
    port: int | None = None,
    store: SecretStore | None = None,
) -> dict[str, Any]:
    settings = service.settings
    store = store or key_store()
    kd_path = find_debugger("kd", directory=settings.debuggers_dir)
    network, host_ip = host_address(settings)
    chosen = choose_port(settings, vm, requested=port)
    transport = PowerShellDirectTransport(service.backend, settings)
    transport.health(vm)  # Fail before touching the host if the guest is unreachable.

    prepared = service._run(
        "debug_kdnet_prepare", "ssh_network_prepare",
        {
            "vm": vm, "switch_name": SWITCH_NAME, "host_ip": host_ip,
            "prefix_length": network.prefixlen, "adapter_name": SWITCH_NAME,
            "use_existing_adapter": use_existing_adapter,
        },
        vm_name=vm, audit=False,
    )

    key = generate_key()
    store.write(vm, key)
    execution = transport.exec(
        vm,
        guest_command(host_ip, chosen, key),
        timeout=settings.timeout_seconds,
        max_output=settings.max_output_chars,
    )
    failure = _marker(execution.output, ERROR_MARKER)
    if failure or not execution.success:
        store.delete(vm)
        raise BackendError(
            "The guest refused the KDNET settings: "
            f"{failure or execution.output.strip()[:400]}",
            code="kdnet_guest_failed",
        )
    settings_text = _marker(execution.output, OK_MARKER) or ""

    rule_name = firewall_rule_name(vm)
    state = {
        "vm": vm,
        "port": chosen,
        "host_ip": host_ip,
        "switch_name": SWITCH_NAME,
        "adapter_name": str(prepared.get("adapter_name") or SWITCH_NAME),
        "adapter_created": bool(prepared.get("adapter_created")),
        "host_address_created": bool(prepared.get("host_address_created")),
        "firewall_rule": rule_name,
        "debugger": str(kd_path),
        "key_target": KDNET_TARGET_PREFIX + vm,
        "guest_dbgsettings": settings_text,
        "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_json(kdnet_state_path(settings, vm), {**state, "firewall_applied": False})
    firewall = service._run(
        "debug_kdnet_prepare", "kdnet_firewall_apply",
        {
            "vm": vm, "rule_name": rule_name, "port": chosen,
            "program": str(kd_path), "interface_alias": f"vEthernet ({SWITCH_NAME})",
        },
        vm_name=vm, audit=False,
    )
    write_json(kdnet_state_path(settings, vm), {**state, "firewall_applied": True})
    return {
        "vm": vm,
        "transport": "kdnet",
        "port": chosen,
        "host_ip": host_ip,
        "switch_name": SWITCH_NAME,
        "adapter_name": state["adapter_name"],
        "adapter_created": state["adapter_created"],
        "host_address_created": state["host_address_created"],
        "firewall_rule": firewall.get("rule_name", rule_name),
        "firewall_created": bool(firewall.get("created")),
        "key_stored": "credential_manager",
        "guest_dbgsettings": settings_text,
        "reboot_required": True,
    }


def status(service: Any, vm: str, *, store: SecretStore | None = None) -> dict[str, Any]:
    settings = service.settings
    store = store or key_store()
    state = read_state(settings, vm)
    result: dict[str, Any] = {
        "vm": vm,
        "prepared": state is not None,
        "key_stored": store.has(vm),
    }
    if state is not None:
        result.update(
            {
                "port": state.get("port"),
                "host_ip": state.get("host_ip"),
                "switch_name": state.get("switch_name"),
                "firewall_rule": state.get("firewall_rule"),
                "firewall_applied": bool(state.get("firewall_applied")),
                "prepared_at": state.get("prepared_at"),
                "guest_dbgsettings": state.get("guest_dbgsettings"),
            }
        )
    return result


def deprovision(
    service: Any, vm: str, *, store: SecretStore | None = None
) -> dict[str, Any]:
    settings = service.settings
    store = store or key_store()
    state = read_state(settings, vm) or {}
    rule_name = str(state.get("firewall_rule") or firewall_rule_name(vm))
    removed = service._run(
        "debug_kdnet_deprovision", "kdnet_firewall_remove",
        {"vm": vm, "rule_name": rule_name}, vm_name=vm, audit=False,
    )
    transport = PowerShellDirectTransport(service.backend, settings)
    guest: dict[str, Any] = {}
    try:
        execution = transport.exec(
            vm, removal_command(),
            timeout=settings.timeout_seconds, max_output=settings.max_output_chars,
        )
        guest = {
            "success": execution.success,
            "error": _marker(execution.output, ERROR_MARKER),
        }
    except Exception as exc:  # the VM may be off; the host side still gets cleaned
        guest = {"success": False, "error": type(exc).__name__}
    key_deleted = store.delete(vm)
    kdnet_state_path(settings, vm).unlink(missing_ok=True)
    return {
        "vm": vm,
        "firewall_removed": bool(removed.get("removed")),
        "key_deleted": bool(key_deleted),
        "guest": guest,
        "reboot_required": True,
    }


__all__ = [
    "ERROR_MARKER",
    "FIREWALL_RULE_PREFIX",
    "OK_MARKER",
    "PORT_MAX",
    "PORT_MIN",
    "choose_port",
    "deprovision",
    "firewall_rule_name",
    "generate_key",
    "guest_command",
    "key_store",
    "prepare",
    "read_state",
    "removal_command",
    "status",
]
