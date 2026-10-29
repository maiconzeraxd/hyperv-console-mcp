"""Guest automation settings: autologon, lock screen, power, UAC, updates.

The guest script owns every change and records the value it replaced in
``C:\\DriverLab\\State\\guest-config.json``; this module only ships the script,
writes the settings document and reads the result back. The autologon password
travels in that settings document, which the guest deletes before it touches
anything, so the secret never appears on a command line.
"""

from __future__ import annotations

import base64
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ..errors import BackendError

GUEST_SCRIPT = r"C:\ProgramData\HyperVConsoleMCP\guest_config.ps1"
GUEST_SETTINGS_ROOT = r"C:\ProgramData\HyperVConsoleMCP"
GUEST_STATE_PATH = r"C:\DriverLab\State\guest-config.json"

# A complete dump needs the pagefile to cover RAM plus roughly 257 MB.
COMPLETE_DUMP_OVERHEAD_BYTES = 257 * 1024 * 1024
DUMP_MODES = {0: "none", 1: "complete", 2: "kernel", 7: "automatic"}


def _dword_is(value: Any, expected: int) -> bool:
    """True only for an integer equal to ``expected``; a bool or a string is not one."""
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _dump_mode(value: Any) -> str:
    """The name of a ``CrashDumpEnabled`` value; bcdedit's own numbering."""
    if value is None:
        return "none"
    try:
        return DUMP_MODES.get(int(value), "other")
    except (TypeError, ValueError):
        return "other"


def derive_live(facts: dict[str, Any]) -> dict[str, Any]:
    """Turn the guest's raw facts into the live view the tools report.

    The guest reports registry values, BCD tokens and two memory figures; every
    "is this on" in the answer is decided here, where it can be tested without a
    VM. A missing value stays null rather than becoming False — absent and zero
    are different facts — except where the guest has always read absence as the
    Windows default: a guest without ``DisableCAD`` does require Ctrl+Alt+Del,
    and a guest without ``CrashDumpEnabled`` has no dump configured.
    """
    boot = facts.get("boot") or {}
    capacity = facts.get("dump_capacity") or {}
    memory = capacity.get("memory_bytes")
    pagefile = capacity.get("pagefile_bytes")
    sufficient = None
    if isinstance(memory, int) and isinstance(pagefile, int):
        sufficient = pagefile >= memory + COMPLETE_DUMP_OVERHEAD_BYTES
    return {
        "autologon": facts.get("autologon") or {},
        "lock_screen_disabled": _dword_is(facts.get("no_lock_screen"), 1),
        "ctrl_alt_del_required": not _dword_is(facts.get("disable_cad"), 1),
        "uac_consent_prompt_for_admins": facts.get("consent_prompt_behavior_admin"),
        "enable_lua": facts.get("enable_lua"),
        "windows_update_disabled": _dword_is(facts.get("no_auto_update"), 1),
        "windows_update_auto_reboot_blocked": _dword_is(
            facts.get("no_auto_reboot_with_logged_on_users"), 1
        ),
        "screensaver_active": facts.get("screen_save_active"),
        "power": facts.get("power") or {},
        "interactive_sessions": facts.get("interactive_sessions") or [],
        "driver_ready": {
            "test_signing": boot.get("testsigning") == "on",
            "kernel_debug": boot.get("debug") == "on",
            "crash_dump_enabled": boot.get("crash_dump_enabled"),
            "crash_dump_mode": _dump_mode(boot.get("crash_dump_enabled")),
            "hvci_enabled": _dword_is(boot.get("hvci_enabled"), 1),
            "vbs_enabled": _dword_is(boot.get("vbs_enabled"), 1),
            "dump_support": {
                "memory_bytes": memory,
                "pagefile_bytes": pagefile,
                "sufficient_for_complete_dump": sufficient,
            },
        },
    }


def _marker_result(output: str) -> dict[str, Any]:
    from .ssh import ERROR_MARKER, JSON_MARKER

    for line in output.splitlines():
        if line.startswith(JSON_MARKER):
            return json.loads(base64.b64decode(line[len(JSON_MARKER):]).decode("utf-8"))
        if line.startswith(ERROR_MARKER):
            raise BackendError(
                f"The guest rejected the configuration change: {line[len(ERROR_MARKER):]}",
                code="guest_config_failed",
            )
    raise BackendError(
        f"The guest configuration script produced no result: {output.strip()[:400]}",
        code="guest_config_failed",
    )


def _run(service: Any, vm: str, transport: Any, arguments: str) -> dict[str, Any]:
    command = (
        "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        f"-File {_quote(GUEST_SCRIPT)} {arguments} -StatePath {_quote(GUEST_STATE_PATH)}"
    )
    execution = transport.exec(
        vm,
        command,
        timeout=service.settings.timeout_seconds,
        max_output=service.settings.max_output_chars,
    )
    result = _marker_result(execution.output)
    if isinstance(result.get("live"), dict):
        result["live"] = derive_live(result["live"])
    return result


def _quote(value: str) -> str:
    from .ssh import ps_quote

    return ps_quote(value)


def _upload_script(transport: Any, vm: str) -> None:
    source = Path(__file__).with_name("guest_config.ps1")
    transport.put_file(vm, source, GUEST_SCRIPT, overwrite=True)


def apply(
    service: Any,
    vm: str,
    *,
    autologon: dict[str, Any] | None,
    lock_screen: bool | None,
    disable_cad: bool | None,
    power: bool | None,
    uac: bool | None,
    windows_update: bool | None,
) -> dict[str, Any]:
    """Ship the script and the settings document, then let the guest apply them.

    Each toggle is tri-state: ``True`` applies it, ``False`` restores the value
    it replaced, and ``None`` leaves that setting alone.
    """
    return _apply_document(service, vm, {
        "autologon": autologon,
        "lock_screen": lock_screen,
        "disable_cad": disable_cad,
        "power": power,
        "uac": uac,
        "windows_update": windows_update,
    })


def lab_apply(
    service: Any,
    vm: str,
    *,
    testsigning: bool | None,
    kernel_debug: bool | None,
    crash_dump: str | bool | None,
    hvci: bool | None,
) -> dict[str, Any]:
    """Driver-lab boot entries; the same tri-state rules as the automation toggles.

    ``crash_dump`` is a mode rather than a toggle: ``kernel``, ``complete`` or
    ``none`` applies it and ``False`` restores the mode it replaced.
    """
    return _apply_document(service, vm, {
        "testsigning": testsigning,
        "kernel_debug": kernel_debug,
        "crash_dump": crash_dump,
        "hvci": hvci,
    })


def _apply_document(service: Any, vm: str, document: dict[str, Any]) -> dict[str, Any]:
    transport = service._transport(vm)
    transport.health(vm)  # Fail before uploading anything if the guest is unreachable.
    _upload_script(transport, vm)

    guest_settings = f"{GUEST_SETTINGS_ROOT}\\guest-config-{uuid.uuid4().hex}.json"
    directory = Path(tempfile.mkdtemp(prefix="hvmcp-guest-config-"))
    local = directory / "settings.json"
    try:
        local.write_text(json.dumps(document), encoding="utf-8")
        transport.put_file(vm, local, guest_settings, overwrite=False)
    finally:
        # The local copy holds the password too.
        shutil.rmtree(directory, ignore_errors=True)
    try:
        return _run(service, vm, transport, f"-Mode Apply -SettingsPath {_quote(guest_settings)}")
    except Exception:
        # The guest deletes the file itself on success; on failure it may still
        # be there, so try once more before reporting the error.
        try:
            transport.exec(
                vm,
                f"Remove-Item -LiteralPath {_quote(guest_settings)} -Force -ErrorAction SilentlyContinue",
                timeout=30,
                max_output=4096,
            )
        except Exception:
            pass
        raise


def status(service: Any, vm: str) -> dict[str, Any]:
    transport = service._transport(vm)
    _upload_script(transport, vm)
    return _run(service, vm, transport, "-Mode Status")


def revert(service: Any, vm: str) -> dict[str, Any]:
    transport = service._transport(vm)
    transport.health(vm)
    _upload_script(transport, vm)
    return _run(service, vm, transport, "-Mode Revert")


__all__ = [
    "COMPLETE_DUMP_OVERHEAD_BYTES", "DUMP_MODES",
    "GUEST_SCRIPT", "GUEST_SETTINGS_ROOT", "GUEST_STATE_PATH",
    "apply", "derive_live", "lab_apply", "revert", "status",
]
