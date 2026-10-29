"""The keyboard, the mouse-free display size, and the refusals around them."""

from __future__ import annotations

from typing import Any

import pytest

from hyperv_console_mcp.errors import BackendError
from hyperv_console_mcp.hostwmi import WmiBackend
from hyperv_console_mcp.hostwmi.connection import WmiFailure
from hyperv_console_mcp.hostwmi.console import (
    console_ctrl_alt_delete,
    console_info,
    console_key_state,
    console_press_keys,
    console_type_text,
)

VM_ID = "D5AD7D1D-5CB3-441F-A9D7-138640C8F61A"
KEYBOARD_PATH = (
    r"\\HOST\root\virtualization\v2:Msvm_Keyboard.CreationClassName="
    f'"Msvm_Keyboard",DeviceID="Microsoft:DE6CDC86",SystemName="{VM_ID}"'
)


class FakeConnection:
    def __init__(
        self,
        devices: dict[str, list[dict[str, Any]]] | None = None,
        *,
        return_value: int = 0,
        fail_at: int | None = None,
    ) -> None:
        self.devices = {"Msvm_Keyboard": [_device("Msvm_Keyboard")], **(devices or {})}
        self.return_value = return_value
        self.fail_at = fail_at
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def query(self, wql: str, *, namespace: str = "") -> list[dict[str, Any]]:
        if "FROM Msvm_ComputerSystem" in wql:
            return [_computer()]
        for class_name, rows in self.devices.items():
            if f"FROM {class_name}" in wql:
                return rows
        raise AssertionError(f"unexpected query: {wql}")

    def get(self, path: str, *, namespace: str = "") -> dict[str, Any]:
        raise AssertionError("the console does not read objects back")

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        self.calls.append((path, method, parameters))
        if self.return_value or len(self.calls) == self.fail_at:
            return {"ReturnValue": self.return_value or 32775}
        return {"ReturnValue": 0}

    def methods(self) -> list[str]:
        return [method for _, method, _ in self.calls]


def _computer(**overrides: Any) -> dict[str, Any]:
    row = {
        "ElementName": "NORTH-Test",
        "Name": VM_ID,
        "EnabledState": 2,
        "StatusDescriptions": [],
        "OnTimeInMilliseconds": 0,
    }
    row.update(overrides)
    return row


def _device(class_name: str, **overrides: Any) -> dict[str, Any]:
    row = {"__PATH": KEYBOARD_PATH, "SystemName": VM_ID, "DeviceID": "Microsoft:DE6CDC86"}
    row.update(overrides)
    return row


def _head(horizontal: int, vertical: int) -> dict[str, Any]:
    return {
        "CurrentHorizontalResolution": horizontal,
        "CurrentVerticalResolution": vertical,
    }


def test_console_info_reports_the_guest_display() -> None:
    connection = FakeConnection({"Msvm_VideoHead": [_head(1024, 768)]})
    assert console_info(connection, {"vm": "NORTH-Test"}) == {
        "vm": "NORTH-Test",
        "state": "Running",
        "video_heads": [{"horizontal": 1024, "vertical": 768}],
    }


def test_console_info_without_a_head_lists_nothing() -> None:
    """A guest that has not started its video device is not an error here."""
    result = console_info(FakeConnection({"Msvm_VideoHead": []}), {"vm": "NORTH-Test"})
    assert result["video_heads"] == []


def test_type_text_sends_newlines_as_enter() -> None:
    connection = FakeConnection()
    result = console_type_text(connection, {"vm": "NORTH-Test", "text": "a\r\nb\nc\rd"})
    assert result == {"vm": "NORTH-Test", "chunks_sent": 4, "completed": True}
    assert connection.calls[1][1] == "TypeKey"
    assert connection.calls[1][2] == {"KeyCode": 0x0D}
    typed = [call[2]["AsciiText"] for call in connection.calls if call[1] == "TypeText"]
    assert typed == ["a", "b", "c", "d"]


def test_type_text_chunks_at_the_type_text_limit() -> None:
    connection = FakeConnection()
    result = console_type_text(connection, {"vm": "NORTH-Test", "text": "x" * 600})
    assert result["chunks_sent"] == 3
    lengths = [len(call[2]["AsciiText"]) for call in connection.calls if call[1] == "TypeText"]
    assert lengths == [256, 256, 88]


def test_empty_text_sends_nothing() -> None:
    connection = FakeConnection()
    assert console_type_text(connection, {"vm": "NORTH-Test", "text": ""})["chunks_sent"] == 0
    assert connection.calls == []


def test_press_keys_presses_in_order_and_releases_in_reverse() -> None:
    connection = FakeConnection()
    result = console_press_keys(connection, {"vm": "NORTH-Test", "keys": ["ctrl", "shift", "escape"]})
    assert result == {"vm": "NORTH-Test", "keys_sent": 3, "completed": True}
    assert [(call[1], call[2]["KeyCode"]) for call in connection.calls] == [
        ("PressKey", 0x11),
        ("PressKey", 0x10),
        ("PressKey", 0x1B),
        ("ReleaseKey", 0x1B),
        ("ReleaseKey", 0x10),
        ("ReleaseKey", 0x11),
    ]


def test_a_refused_press_still_releases_what_was_pressed() -> None:
    """A stuck modifier would leak into the next request."""
    connection = FakeConnection(fail_at=2)
    with pytest.raises(WmiFailure):
        console_press_keys(connection, {"vm": "NORTH-Test", "keys": ["ctrl", "alt"]})
    assert [(call[1], call[2]["KeyCode"]) for call in connection.calls] == [
        ("PressKey", 0x11),
        ("PressKey", 0x12),
        ("ReleaseKey", 0x11),
    ]


def test_an_unknown_key_releases_the_modifiers_already_down() -> None:
    connection = FakeConnection()
    with pytest.raises(WmiFailure) as caught:
        console_press_keys(connection, {"vm": "NORTH-Test", "keys": ["ctrl", "hyper"]})
    assert caught.value.code == "invalid_argument"
    assert "Unsupported key name 'hyper'." in str(caught.value)
    assert connection.methods() == ["PressKey", "ReleaseKey"]


def test_key_state_presses_and_releases_one_key() -> None:
    down = FakeConnection()
    assert console_key_state(down, {"vm": "NORTH-Test", "key": "win", "down": True}) == {
        "vm": "NORTH-Test",
        "key": "win",
        "down": True,
    }
    assert down.methods() == ["PressKey"]
    up = FakeConnection()
    console_key_state(up, {"vm": "NORTH-Test", "key": "win", "down": False})
    assert up.methods() == ["ReleaseKey"]


def test_ctrl_alt_delete_is_one_method() -> None:
    connection = FakeConnection()
    assert console_ctrl_alt_delete(connection, {"vm": "NORTH-Test"}) == {
        "vm": "NORTH-Test",
        "completed": True,
    }
    assert connection.methods() == ["TypeCtrlAltDel"]


def test_a_refused_method_keeps_the_bridge_wording() -> None:
    connection = FakeConnection(return_value=32768)
    with pytest.raises(WmiFailure) as caught:
        console_ctrl_alt_delete(connection, {"vm": "NORTH-Test"})
    assert "Hyper-V WMI method TypeCtrlAltDel returned 32768." in str(caught.value)


def test_a_vm_without_a_keyboard_is_not_found() -> None:
    connection = FakeConnection({"Msvm_Keyboard": []})
    with pytest.raises(WmiFailure) as caught:
        console_type_text(connection, {"vm": "NORTH-Test", "text": "a"})
    assert caught.value.code == "not_found"
    assert "Expected one Msvm_Keyboard for" in str(caught.value)


def test_the_backend_serves_the_keyboard() -> None:
    backend = WmiBackend(connect=lambda: FakeConnection())
    assert backend.invoke("console_ctrl_alt_delete", {"vm": "NORTH-Test"})["completed"] is True


def test_the_mouse_is_still_the_workers() -> None:
    backend = WmiBackend(connect=lambda: FakeConnection())
    with pytest.raises(BackendError) as caught:
        backend.invoke("console_mouse_move", {"vm": "NORTH-Test", "x": 1, "y": 2})
    assert caught.value.code == "wmi_unsupported"
