"""Driver Verifier in the guest: read it, turn it on, turn it off, put it back.

The guest program owns every change and records the values it replaced in
``C:\\DriverLab\\State\\verifier.json``; this module ships the program, checks
the arguments, and decides what the numbers it reports mean.

Verifier keeps its setting in two registry values — ``VerifyDriverLevel``, a
bitmask, and ``VerifyDrivers``, the list of drivers it applies to — which is why
nothing here reads ``verifier /query``: its output is translated, and a pt-BR
guest would answer an English parser with nothing at all. The raw query text is
still returned for the operator to read.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..errors import ValidationError
from . import guest_program

GUEST_SCRIPT = Path(__file__).with_name("guest_verifier.ps1")
GUEST_STATE_PATH = r"C:\DriverLab\State\verifier.json"

# The bits verifier's own command-line reference documents. A mask that came
# from `verifier /standard` carries more than this, and an unrecognised bit is
# reported by number rather than given a name that might be the wrong one.
FLAG_NAMES: dict[int, str] = {
    0x00000001: "special_pool",
    0x00000002: "pool_tracking",
    0x00000004: "force_irql_checking",
    0x00000008: "low_resources_simulation",
    0x00000010: "pool_checking",
    0x00000020: "ddi_compliance",
    0x00000080: "pending_io",
    0x00000100: "irp_logging",
}
FLAG_WORDS = ("standard", "all")
ACTIONS = ("enable", "disable", "revert")
_DRIVER = re.compile(r"[A-Za-z0-9._-]{1,120}")
_HEX = re.compile(r"0[xX]([0-9a-fA-F]{1,8})")


def normalise_flags(value: str | None) -> str:
    """``standard``, ``all``, or a mask written the way the guest will pass it."""
    if value is None:
        return "standard"
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("flags must be standard, all, or a numeric mask.")
    text = value.strip()
    if text.casefold() in FLAG_WORDS:
        return text.casefold()
    match = _HEX.fullmatch(text)
    if match:
        return f"0x{int(match.group(1), 16):08x}"
    if text.isdigit():
        number = int(text)
        if 0 <= number <= 0xFFFFFFFF:
            return f"0x{number:08x}"
    raise ValidationError("flags must be standard, all, or a mask between 0 and 0xFFFFFFFF.")


def normalise_drivers(value: Any) -> list[str]:
    """The driver names to verify, each one a name and never a path."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [part for part in re.split(r"[,\s]+", value) if part]
    if not isinstance(value, (list, tuple)):
        raise ValidationError("drivers must be a list of driver names.")
    drivers: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValidationError("drivers must be a list of driver names.")
        name = item.strip()
        if not _DRIVER.fullmatch(name) or name.strip(".") == "":
            raise ValidationError(
                f"{name!r} is not a driver name. Use the file name, such as mydriver.sys, "
                "not a path; verifier matches it against the module name."
            )
        if name not in drivers:
            drivers.append(name)
    if len(drivers) > 32:
        raise ValidationError("At most 32 driver names can be given at once.")
    return drivers


def derive(result: dict[str, Any]) -> dict[str, Any]:
    """Turn the guest's raw facts into the view the tools report.

    ``enabled`` is decided here, from the mask, so the same reading of the same
    number is used by every caller and can be tested without a guest.
    """
    live = result.get("live")
    if not isinstance(live, dict):
        return result
    mask = live.get("flags_mask")
    if isinstance(mask, bool) or not isinstance(mask, int):
        mask = None
    named: list[str] = []
    unnamed: list[str] = []
    if mask is not None:
        covered = 0
        for bit, name in FLAG_NAMES.items():
            covered |= bit
            if mask & bit:
                named.append(name)
        unknown = mask & ~covered
        for bit in range(32):
            value = 1 << bit
            if unknown & value:
                unnamed.append(f"0x{value:08x}")
    live["flags_mask"] = mask
    live["flags_hex"] = f"0x{mask:08x}" if mask is not None else None
    live["flags_named"] = named
    live["unnamed_bits"] = unnamed
    live["enabled"] = bool(mask)
    result["enabled"] = bool(mask)
    return result


def _run(service: Any, vm: str, arguments: list[str]) -> dict[str, Any]:
    transport = service._transport(vm)
    return derive(guest_program.run(
        service, vm, transport,
        script=GUEST_SCRIPT,
        arguments=[*arguments, "-StatePath", guest_program.quote(GUEST_STATE_PATH)],
        what="the verifier change",
        code="guest_verifier_failed",
    ))


def status(service: Any, vm: str) -> dict[str, Any]:
    return _run(service, vm, ["-Mode", "Status"])


def apply(
    service: Any,
    vm: str,
    action: str,
    *,
    drivers: Any = None,
    flags: str | None = None,
) -> dict[str, Any]:
    """Enable, disable, or revert; only ``enable`` takes drivers and flags."""
    if action not in ACTIONS:
        raise ValidationError(f"action must be one of: {', '.join(ACTIONS)}.")
    if action != "enable":
        if drivers or flags is not None:
            raise ValidationError(f"drivers and flags apply to action=enable only, not {action}.")
        return _run(service, vm, ["-Mode", action.capitalize()])
    names = normalise_drivers(drivers)
    mask = normalise_flags(flags)
    transport = service._transport(vm)
    # Fail before uploading anything if the guest is unreachable.
    transport.health(vm)
    return derive(guest_program.run(
        service, vm, transport,
        script=GUEST_SCRIPT,
        arguments=[
            "-Mode", "Enable",
            "-Flags", guest_program.quote(mask),
            "-Drivers", guest_program.quote(",".join(names)),
            "-StatePath", guest_program.quote(GUEST_STATE_PATH),
        ],
        what="the verifier change",
        code="guest_verifier_failed",
    ))


__all__ = [
    "ACTIONS", "FLAG_NAMES", "FLAG_WORDS", "GUEST_SCRIPT", "GUEST_STATE_PATH",
    "apply", "derive", "normalise_drivers", "normalise_flags", "status",
]
