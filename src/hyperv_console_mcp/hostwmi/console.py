"""Synthetic keyboard input and the guest display size.

``Msvm_Keyboard``, ``Msvm_SyntheticMouse`` and ``Msvm_VideoHead`` are per-VM
device instances, and each one carries the VM's own GUID in ``SystemName`` — so
finding a device is one query, not the association walk the bridge does.

The keyboard methods answer ``0`` or a refusal directly; unlike the management
service's, they start no job. Text is typed in chunks of 256 characters, the
limit ``TypeText`` documents, and a newline becomes ``VK_RETURN``.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .connection import Connection, WmiFailure
from .vm import VM_STATES, _int, find_vm

# Virtual-key codes, as the bridge maps the tool's key names. The letters and
# digits are added below so the table stays readable.
KEY_CODES: dict[str, int] = {
    "ctrl": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "win": 0x5B,
    "enter": 0x0D,
    "escape": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
}
KEY_CODES.update({f"f{number}": 0x70 + number - 1 for number in range(1, 13)})
KEY_CODES.update({str(digit): 0x30 + digit for digit in range(10)})
KEY_CODES.update({chr(0x61 + index): 0x41 + index for index in range(26)})

VK_RETURN = 0x0D
TEXT_CHUNK = 256
CHUNK_PAUSE_SECONDS = 0.015
_NEWLINE = re.compile(r"(\r\n|\r|\n)")


def console_info(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = find_vm(connection, str(payload.get("vm") or ""))
    identifier = str(vm.get("Name") or "")
    return {
        "vm": vm.get("ElementName"),
        "state": VM_STATES.get(_int(vm.get("EnabledState")), "Other"),
        "video_heads": [
            {
                "horizontal": _int(head.get("CurrentHorizontalResolution")),
                "vertical": _int(head.get("CurrentVerticalResolution")),
            }
            for head in _devices(connection, identifier, "Msvm_VideoHead")
        ],
    }


def console_type_text(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = find_vm(connection, str(payload.get("vm") or ""))
    keyboard = _device(connection, str(vm.get("Name") or ""), "Msvm_Keyboard")
    chunks = _type_text(connection, keyboard, str(payload.get("text") or ""))
    return {"vm": vm.get("ElementName"), "chunks_sent": chunks, "completed": True}


def console_press_keys(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = find_vm(connection, str(payload.get("vm") or ""))
    keyboard = _device(connection, str(vm.get("Name") or ""), "Msvm_Keyboard")
    keys = [str(key) for key in payload.get("keys") or []]
    return {"vm": vm.get("ElementName"), "keys_sent": _chord(connection, keyboard, keys), "completed": True}


def console_key_state(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = find_vm(connection, str(payload.get("vm") or ""))
    keyboard = _device(connection, str(vm.get("Name") or ""), "Msvm_Keyboard")
    key = str(payload.get("key") or "")
    down = bool(payload.get("down"))
    _invoke(connection, keyboard, "PressKey" if down else "ReleaseKey", KeyCode=_key_code(key))
    return {"vm": vm.get("ElementName"), "key": key, "down": down}


def console_ctrl_alt_delete(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = find_vm(connection, str(payload.get("vm") or ""))
    keyboard = _device(connection, str(vm.get("Name") or ""), "Msvm_Keyboard")
    _invoke(connection, keyboard, "TypeCtrlAltDel")
    return {"vm": vm.get("ElementName"), "completed": True}


def _type_text(connection: Connection, keyboard: dict[str, Any], text: str) -> int:
    """Type the text, sending one ``VK_RETURN`` per newline.

    ``re.split`` keeps the separators, so a ``\\r\\n`` stays one separator rather
    than becoming two returns.
    """
    chunks = 0
    for segment in _NEWLINE.split(text):
        if _NEWLINE.fullmatch(segment):
            _invoke(connection, keyboard, "TypeKey", KeyCode=VK_RETURN)
            continue
        for offset in range(0, len(segment), TEXT_CHUNK):
            _invoke(
                connection, keyboard, "TypeText", AsciiText=segment[offset : offset + TEXT_CHUNK]
            )
            chunks += 1
            time.sleep(CHUNK_PAUSE_SECONDS)
    return chunks


def _chord(connection: Connection, keyboard: dict[str, Any], keys: list[str]) -> int:
    """Press the keys in order and release them in reverse, even on failure.

    A modifier left down would leak into the next request, so the release runs
    in ``finally`` and each release is attempted even if an earlier one fails.
    """
    pressed: list[int] = []
    try:
        for key in keys:
            code = _key_code(key)
            _invoke(connection, keyboard, "PressKey", KeyCode=code)
            pressed.append(code)
    finally:
        failure: WmiFailure | None = None
        for code in reversed(pressed):
            try:
                _invoke(connection, keyboard, "ReleaseKey", KeyCode=code)
            except WmiFailure as exc:
                failure = failure or exc
        if failure is not None:
            raise failure
    return len(pressed)


def _key_code(name: str) -> int:
    code = KEY_CODES.get(name)
    if code is None:
        raise WmiFailure(f"Unsupported key name '{name}'.", code="invalid_argument")
    return code


def _device(connection: Connection, identifier: str, class_name: str) -> dict[str, Any]:
    devices = _devices(connection, identifier, class_name)
    if len(devices) != 1:
        raise WmiFailure(
            f"Expected one {class_name} for '{identifier}'; found {len(devices)}.",
            code="not_found",
        )
    return devices[0]


def _devices(connection: Connection, identifier: str, class_name: str) -> list[dict[str, Any]]:
    """The VM's instances of one device class, keyed by ``SystemName``."""
    return connection.query(
        f"SELECT * FROM {class_name} WHERE SystemName = '{_literal(identifier)}'"
    )


def _invoke(
    connection: Connection, device: dict[str, Any], method: str, **arguments: Any
) -> None:
    response = connection.call(str(device["__PATH"]), method, **arguments)
    code = _int(response.get("ReturnValue"))
    if code != 0:
        raise WmiFailure(f"Hyper-V WMI method {method} returned {code}.")


def _literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "''")
