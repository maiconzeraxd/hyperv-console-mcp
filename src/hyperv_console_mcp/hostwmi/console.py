"""Synthetic keyboard and mouse input, and the guest display size.

``Msvm_Keyboard``, ``Msvm_SyntheticMouse`` and ``Msvm_VideoHead`` are per-VM
device instances, and each one carries the VM's own GUID in ``SystemName`` — so
finding a device is one query, not the association walk the bridge does.

The input methods answer ``0`` or a refusal directly; unlike the management
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


def console_mouse_move(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = find_vm(connection, str(payload.get("vm") or ""))
    mouse = _device(connection, str(vm.get("Name") or ""), "Msvm_SyntheticMouse")
    x = _int(payload.get("x"))
    y = _int(payload.get("y"))
    _move(connection, mouse, x, y)
    return {"vm": vm.get("ElementName"), "x": x, "y": y}


def console_click(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = find_vm(connection, str(payload.get("vm") or ""))
    mouse = _device(connection, str(vm.get("Name") or ""), "Msvm_SyntheticMouse")
    if "x" in payload:
        _move(connection, mouse, _int(payload.get("x")), _int(payload.get("y")))
    button = _int(payload.get("button_index"))
    _invoke(connection, mouse, "ClickButton", ButtonIndex=button)
    return {"vm": vm.get("ElementName"), "button_index": button, "completed": True}


def console_mouse_buttons(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = find_vm(connection, str(payload.get("vm") or ""))
    mouse = _device(connection, str(vm.get("Name") or ""), "Msvm_SyntheticMouse")
    states = _buttons(connection, mouse, payload)
    return {"vm": vm.get("ElementName"), **states}


def console_scroll(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = find_vm(connection, str(payload.get("vm") or ""))
    mouse = _device(connection, str(vm.get("Name") or ""), "Msvm_SyntheticMouse")
    delta = _int(payload.get("delta"))
    _invoke(connection, mouse, "SetScrollPosition", ScrollPositionDelta=delta)
    return {"vm": vm.get("ElementName"), "delta": delta, "completed": True}


def console_batch(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    """Run an ordered batch with one device lookup per device.

    The whole batch is one request so an intermediate state cannot be
    interleaved with another console call, which is why it stays here rather
    than in the caller.
    """
    vm = find_vm(connection, str(payload.get("vm") or ""))
    identifier = str(vm.get("Name") or "")
    keyboard: dict[str, Any] | None = None
    mouse: dict[str, Any] | None = None
    results: list[dict[str, Any]] = []
    for index, action in enumerate(payload.get("actions") or []):
        kind = str(action.get("type") or "")
        if kind in _KEYBOARD_ACTIONS and keyboard is None:
            keyboard = _device(connection, identifier, "Msvm_Keyboard")
        if kind in _MOUSE_ACTIONS and mouse is None:
            mouse = _device(connection, identifier, "Msvm_SyntheticMouse")
        results.append(
            {
                "index": index,
                "type": kind,
                "result": _batch_entry(connection, kind, action, keyboard, mouse),
            }
        )
    return {
        "vm": vm.get("ElementName"),
        "actions_completed": len(results),
        "results": results,
    }


def _batch_entry(
    connection: Connection,
    kind: str,
    action: dict[str, Any],
    keyboard: dict[str, Any] | None,
    mouse: dict[str, Any] | None,
) -> dict[str, Any]:
    if kind == "text":
        chunks = _type_text(connection, _require(keyboard), str(action.get("text") or ""))
        return {"chunks_sent": chunks, "completed": True}
    if kind == "keys":
        keys = [str(key) for key in action.get("keys") or []]
        return {"keys_sent": _chord(connection, _require(keyboard), keys), "completed": True}
    if kind == "key_state":
        key = str(action.get("key") or "")
        down = bool(action.get("down"))
        method = "PressKey" if down else "ReleaseKey"
        _invoke(connection, _require(keyboard), method, KeyCode=_key_code(key))
        return {"key": key, "down": down, "completed": True}
    if kind == "ctrl_alt_delete":
        _invoke(connection, _require(keyboard), "TypeCtrlAltDel")
        return {"completed": True}
    if kind == "mouse_buttons":
        return {**_buttons(connection, _require(mouse), action), "completed": True}
    if kind == "move":
        x = _int(action.get("x"))
        y = _int(action.get("y"))
        _move(connection, _require(mouse), x, y)
        return {"x": x, "y": y}
    if kind == "click":
        if "x" in action:
            _move(connection, _require(mouse), _int(action.get("x")), _int(action.get("y")))
        button = _int(action.get("button_index"))
        _invoke(connection, _require(mouse), "ClickButton", ButtonIndex=button)
        return {"button_index": button, "completed": True}
    if kind == "scroll":
        delta = _int(action.get("delta"))
        _invoke(connection, _require(mouse), "SetScrollPosition", ScrollPositionDelta=delta)
        return {"delta": delta, "completed": True}
    if kind == "wait":
        milliseconds = _int(action.get("milliseconds"))
        time.sleep(milliseconds / 1000)
        return {"waited_ms": milliseconds}
    raise WmiFailure(f"Unsupported console action '{kind}'.", code="invalid_argument")


def _buttons(
    connection: Connection, mouse: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """All three button states in one pass: left, right, middle are 1, 2, 3.

    The second parameter is ``IsDown``, not ``ButtonState`` — the bridge passed
    the latter, which the provider refuses, so that call never worked there.

    The answer carries no ``completed`` flag because the bridge's standalone
    ``console_mouse_buttons`` does not either; its batch entry adds one.
    """
    states = {name: bool(payload.get(name)) for name in ("left", "right", "middle")}
    for index, state in enumerate(states.values(), start=1):
        _invoke(connection, mouse, "SetButtonState", ButtonIndex=index, IsDown=state)
    return states


def _move(connection: Connection, mouse: dict[str, Any], x: int, y: int) -> None:
    _invoke(
        connection, mouse, "SetAbsolutePosition", HorizontalPosition=x, VerticalPosition=y
    )


_KEYBOARD_ACTIONS = frozenset({"text", "keys", "key_state", "ctrl_alt_delete"})
_MOUSE_ACTIONS = frozenset({"mouse_buttons", "move", "click", "scroll"})


def _require(device: dict[str, Any] | None) -> dict[str, Any]:
    """The device the action needs; a batch that got here without it is a bug."""
    if device is None:  # pragma: no cover - guarded by the action tables above
        raise WmiFailure("The batch reached an action without its input device.")
    return device


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
