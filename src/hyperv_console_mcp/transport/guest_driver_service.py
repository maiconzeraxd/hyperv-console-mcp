"""A test kernel-driver service in the guest: install, start, stop, remove, read.

The guest program drives the Service Control Manager through advapi32 rather
than ``sc.exe`` or ``New-Service``, because those answer with translated text
and the numbers that matter here are the ones they hide: the Win32 error from
``StartService`` and the driver's own NTSTATUS, which the SCM reports in
``SERVICE_STATUS.dwServiceSpecificExitCode`` when ``dwWin32ExitCode`` is
``ERROR_SERVICE_SPECIFIC_ERROR``.

Nothing here bypasses Driver Signature Enforcement. A driver the guest refuses
to load is reported with the code it was refused with, and test signing stays
the operator's boot setting (``guest_config.ps1``), never a step this tool takes
to make a load succeed.
"""

from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath
from typing import Any

from ..errors import ValidationError
from . import guest_program

GUEST_SCRIPT = Path(__file__).with_name("guest_driver_service.ps1")
GUEST_STATE_PATH = r"C:\DriverLab\State\driver-services.json"
# Only a driver the operator already staged into the lab root can be installed.
IMAGE_ROOT = r"C:\DriverLab"

ACTIONS = ("install", "start", "stop", "remove", "status")
START_TYPES = ("demand", "system", "boot", "auto")
STATES = {
    1: "stopped", 2: "start_pending", 3: "stop_pending", 4: "running",
    5: "continue_pending", 6: "pause_pending", 7: "paused",
}
# ERROR_SERVICE_SPECIFIC_ERROR: the driver's own NTSTATUS is in the second field.
SERVICE_SPECIFIC_ERROR = 1066
# The Win32 errors worth naming. A code outside this table is reported by number,
# which is still the fact; a guessed name would not be.
ERROR_NAMES = {
    2: "file_not_found", 5: "access_denied", 87: "invalid_parameter",
    193: "bad_exe_format", 577: "invalid_image_hash", 1053: "service_request_timeout",
    1058: "service_disabled", 1060: "service_does_not_exist",
    1066: "service_specific_error", 1073: "service_exists",
    1074: "service_cannot_accept_control", 1275: "driver_blocked",
    1279: "driver_failed_prior_unload",
}
# Only the two mappings this module is sure of. Everything else stays a Win32
# code, which is what the API actually returned.
NTSTATUS_FOR_WIN32 = {
    577: 0xC0000428,   # STATUS_INVALID_IMAGE_HASH
    1275: 0xC0000365,  # STATUS_DRIVER_BLOCKED
}
_NAME = re.compile(r"[A-Za-z0-9._-]{1,120}")


def normalise_service_name(value: Any, *, allow_empty: bool = False) -> str:
    """A service name is a name: no path separators, no whitespace."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError("service_name must be a string.")
    name = value.strip()
    if not name and allow_empty:
        return ""
    if not name:
        raise ValidationError("service_name is required.")
    if not _NAME.fullmatch(name) or name.strip(".") == "":
        raise ValidationError(
            f"{name!r} is not a service name. Use letters, digits, dots, dashes and "
            "underscores, such as hvmcp_test."
        )
    return name


def normalise_image_path(value: Any) -> str:
    """The driver image, which must already be a .sys inside the lab root."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError("image_path is required to install a driver service.")
    if not isinstance(value, str):
        raise ValidationError("image_path must be a string.")
    text = value.strip()
    if not text.casefold().endswith(".sys"):
        raise ValidationError("image_path must name a .sys file.")
    path = PureWindowsPath(text)
    if not path.is_absolute() or not path.drive:
        raise ValidationError("image_path must be an absolute Windows path.")
    # PureWindowsPath keeps '..' as a part, so this is checked before the
    # containment test, which would otherwise accept C:\DriverLab\..\x.sys.
    if any(part in ("..", ".") for part in path.parts):
        raise ValidationError("image_path must not contain '.' or '..' segments.")
    root = PureWindowsPath(IMAGE_ROOT)
    if path.drive.casefold() != root.drive.casefold() or not path.is_relative_to(root):
        raise ValidationError(
            f"image_path must be inside {IMAGE_ROOT}, where the lab stages its packages."
        )
    if len(path.parts) <= len(root.parts):
        raise ValidationError("image_path must name a file inside the lab root.")
    return str(path)


def normalise_start_type(value: Any) -> str:
    if value is None:
        return "demand"
    if not isinstance(value, str) or value.strip().casefold() not in START_TYPES:
        raise ValidationError(f"start_type must be one of: {', '.join(START_TYPES)}.")
    return value.strip().casefold()


def request(
    action: Any, service_name: Any, image_path: Any = "", start_type: Any = "demand",
) -> dict[str, Any]:
    """Everything one call will do, checked once, before anything is approved.

    The same values go into the approval summary and into the guest's command
    line, so what the operator is shown is exactly what will run.
    """
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValidationError(f"action must be one of: {', '.join(ACTIONS)}.")
    name = normalise_service_name(service_name, allow_empty=action == "status")
    if action == "install":
        return {
            "action": action,
            "name": name,
            "image_path": normalise_image_path(image_path),
            "start_type": normalise_start_type(start_type),
        }
    if image_path not in ("", None):
        raise ValidationError("image_path applies to action=install only.")
    if normalise_start_type(start_type) != "demand":
        raise ValidationError("start_type applies to action=install only.")
    if action != "status" and not name:
        raise ValidationError(f"service_name is required for action={action}.")
    return {"action": action, "name": name, "image_path": "", "start_type": "demand"}


def derive(result: dict[str, Any]) -> dict[str, Any]:
    """The numbers the guest reported, named here where they can be tested."""
    live = result.get("live")
    if isinstance(live, dict):
        _name_state(live)
    # The action results carry one service under `service`; a status carries a
    # list, each with its own `live`.
    single = result.get("service")
    if isinstance(single, dict):
        _name_state(single)
    for entry in result.get("services") or []:
        if isinstance(entry, dict) and isinstance(entry.get("live"), dict):
            _name_state(entry["live"])
    _name_error(result)
    return result


def _name_state(live: dict[str, Any]) -> None:
    code = live.get("state_code")
    if isinstance(code, int) and not isinstance(code, bool):
        live["state"] = STATES.get(code, f"unknown_{code}")


def _name_error(result: dict[str, Any]) -> None:
    """Name the Win32 error, and carry the driver's NTSTATUS when there is one."""
    live = result.get("service") or result.get("live") or {}
    if not isinstance(live, dict):
        live = {}
    code = result.get("win32_error")
    if isinstance(code, bool) or not isinstance(code, int):
        code = 0
    result["win32_error_name"] = ERROR_NAMES.get(code) if code else None
    ntstatus = None
    exit_code = live.get("service_exit_code")
    if (
        isinstance(live.get("win32_exit_code"), int)
        and live.get("win32_exit_code") == SERVICE_SPECIFIC_ERROR
        and isinstance(exit_code, int) and exit_code > 0
    ):
        ntstatus = exit_code
    elif code in NTSTATUS_FOR_WIN32:
        ntstatus = NTSTATUS_FOR_WIN32[code]
    result["ntstatus"] = f"0x{ntstatus:08X}" if ntstatus is not None else None
    # A read has no outcome to be right or wrong about, so it gets no verdict.
    result["ok"] = (
        bool(result.get("action_ok")) and not code if "action_ok" in result else None
    )


def _arguments(options: dict[str, Any]) -> list[str]:
    action = options["action"]
    arguments = ["-Mode", action.capitalize()]
    if options["name"]:
        arguments += ["-ServiceName", guest_program.quote(options["name"])]
    if action == "install":
        arguments += [
            "-ImagePath", guest_program.quote(options["image_path"]),
            "-StartType", guest_program.quote(options["start_type"]),
        ]
    return [*arguments, "-StatePath", guest_program.quote(GUEST_STATE_PATH)]


def apply(service: Any, vm: str, **options: Any) -> dict[str, Any]:
    """Run the action ``request`` produced; ``service`` is the HyperVService."""
    transport = service._transport(vm)
    # Fail before uploading anything if the guest is unreachable.
    transport.health(vm)
    return derive(guest_program.run(
        service, vm, transport,
        script=GUEST_SCRIPT,
        arguments=_arguments(options),
        what="the driver service change",
        code="guest_driver_service_failed",
    ))


def status(service: Any, vm: str, name: str = "") -> dict[str, Any]:
    return apply(service, vm, **request("status", name))


__all__ = [
    "ACTIONS", "ERROR_NAMES", "GUEST_SCRIPT", "GUEST_STATE_PATH", "IMAGE_ROOT",
    "NTSTATUS_FOR_WIN32", "SERVICE_SPECIFIC_ERROR", "START_TYPES", "STATES",
    "apply", "derive", "normalise_image_path", "normalise_service_name",
    "normalise_start_type", "request", "status",
]
