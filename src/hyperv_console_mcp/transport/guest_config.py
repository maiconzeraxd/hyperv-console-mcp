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
    return _marker_result(execution.output)


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
    transport = service._transport(vm)
    transport.health(vm)  # Fail before uploading anything if the guest is unreachable.
    _upload_script(transport, vm)

    document = {
        "autologon": autologon,
        "lock_screen": lock_screen,
        "disable_cad": disable_cad,
        "power": power,
        "uac": uac,
        "windows_update": windows_update,
    }
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
    "GUEST_SCRIPT", "GUEST_SETTINGS_ROOT", "GUEST_STATE_PATH",
    "apply", "revert", "status",
]
