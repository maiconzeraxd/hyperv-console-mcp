"""Validation and authorization, independent of the live Hyper-V backend."""

from __future__ import annotations

import base64
import binascii
import re
import time
import uuid
from typing import Any, Callable, TypeVar

from .audit import AuditLogger
from .backend import HyperVBackend
from .config import Settings
from .errors import HyperVError, PolicyError, ValidationError
from .serial import SerialManager

MAX_GUEST_FILE_BYTES = 8 * 1024 * 1024
KEY_NAMES = frozenset(
    {
        "ctrl", "shift", "alt", "win", "enter", "escape", "tab", "space",
        "backspace", "delete", "insert", "home", "end", "pageup", "pagedown",
        "up", "down", "left", "right", *(f"f{number}" for number in range(1, 13)),
        *(chr(code) for code in range(ord("a"), ord("z") + 1)),
        *(str(number) for number in range(10)),
    }
)
BUTTONS = {"left": 1, "right": 2, "middle": 3}
T = TypeVar("T")


def _text(value: str, label: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ValidationError(f"{label} must be a non-empty string.")
    if len(value) > maximum or "\x00" in value:
        raise ValidationError(f"{label} exceeds its limit or contains a NUL character.")
    return value


def _integer(value: int, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationError(f"{label} must be an integer between {minimum} and {maximum}.")
    return value


class HyperVService:
    def __init__(
        self,
        backend: HyperVBackend,
        settings: Settings | None = None,
        audit: AuditLogger | None = None,
        serial: SerialManager | None = None,
    ) -> None:
        self.backend = backend
        self.settings = settings or Settings()
        self.audit = audit or AuditLogger(self.settings.audit_log)
        self.serial = serial if serial is not None else SerialManager()

    def _vm(self, name: str) -> str:
        name = _text(name, "vm_name", 256)
        if self.settings.allowed_vms and not self.settings.is_allowed(name):
            raise PolicyError(f"VM {name!r} is not in HYPERV_MCP_ALLOWED_VMS.")
        return name

    def _write(self, name: str, enabled: bool, variable: str) -> str:
        name = self._vm(name)
        if not self.settings.allowed_vms:
            raise PolicyError("Mutations require HYPERV_MCP_ALLOWED_VMS to name the target VMs.")
        if not enabled:
            raise PolicyError(f"This capability is disabled. Set {variable}=true to enable it.")
        return name

    @staticmethod
    def _confirm(actual: str, expected: str, label: str) -> None:
        if actual != expected:
            raise PolicyError(f"{label} must exactly match {expected!r}.")

    def _run(
        self,
        operation: str,
        bridge_operation: str,
        payload: dict[str, Any] | None = None,
        *,
        vm_name: str | None = None,
    ) -> dict[str, Any]:
        try:
            result = self.backend.invoke(
                bridge_operation, payload or {}, timeout=self.settings.timeout_seconds
            )
        except HyperVError as exc:
            self.audit.record(operation, vm_name, "error", exc.code)
            raise
        except Exception:
            self.audit.record(operation, vm_name, "error", "unexpected_error")
            raise
        self.audit.record(operation, vm_name, "success")
        return result

    def _serial_action(
        self, operation: str, vm_name: str, action: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        try:
            result = action()
        except HyperVError as exc:
            self.audit.record(operation, vm_name, "error", exc.code)
            raise
        except Exception:
            self.audit.record(operation, vm_name, "error", "unexpected_error")
            raise
        self.audit.record(operation, vm_name, "success")
        return {"vm": vm_name, **result}

    def vm_wait(
        self,
        vm_name: str,
        states: list[str],
        timeout_seconds: int = 60,
        poll_interval_ms: int = 500,
    ) -> dict[str, Any]:
        name = self._vm(vm_name)
        if not isinstance(states, list) or not states or len(states) > 12:
            raise ValidationError("states must contain between one and twelve VM state names.")
        if any(not isinstance(state, str) or not state.strip() for state in states):
            raise ValidationError("states must contain non-empty strings.")
        timeout = _integer(timeout_seconds, "timeout_seconds", 1, 900)
        interval = _integer(poll_interval_ms, "poll_interval_ms", 100, 10_000)
        deadline = time.monotonic() + timeout
        polls = 0
        while True:
            result = self.get_vm(name)
            polls += 1
            current = str(result["vm"]["state"])
            if current.casefold() in {state.strip().casefold() for state in states}:
                return {"matched": True, "polls": polls, "vm": result["vm"]}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "matched": False, "polls": polls, "vm": result["vm"],
                    "message": "Timed out waiting for the requested state. No power action was taken.",
                }
            time.sleep(min(interval / 1000, remaining))

    def host_status(self) -> dict[str, Any]:
        return self._run("host_status", "host_status")

    def list_vms(self) -> dict[str, Any]:
        result = self._run("vm_list", "vm_list")
        if self.settings.allowed_vms:
            result["vms"] = [
                vm for vm in result.get("vms", [])
                if self.settings.is_allowed(str(vm.get("name", "")))
            ]
        result["allowlist_active"] = bool(self.settings.allowed_vms)
        return result

    def get_vm(self, vm_name: str) -> dict[str, Any]:
        name = self._vm(vm_name)
        return self._run("vm_get", "vm_get", {"vm": name}, vm_name=name)

    def power(self, vm_name: str, action: str, confirm_vm: str | None = None) -> dict[str, Any]:
        name = self._write(vm_name, self.settings.allow_power, "HYPERV_MCP_ALLOW_POWER")
        if action not in {"start", "shutdown", "save", "pause", "resume", "turn_off", "reset"}:
            raise ValidationError(
                "action must be start, shutdown, save, pause, resume, turn_off, or reset."
            )
        if action in {"turn_off", "reset"}:
            self._confirm(confirm_vm or "", name, "confirm_vm")
        return self._run(
            f"vm_power.{action}", "vm_power", {"vm": name, "action": action}, vm_name=name
        )

    def list_checkpoints(self, vm_name: str) -> dict[str, Any]:
        name = self._vm(vm_name)
        return self._run(
            "checkpoint_list", "checkpoint_list", {"vm": name}, vm_name=name
        )

    def create_checkpoint(self, vm_name: str, name: str) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_checkpoints, "HYPERV_MCP_ALLOW_CHECKPOINTS"
        )
        checkpoint_name = _text(name, "name", 128)
        return self._run(
            "checkpoint_create", "checkpoint_create",
            {"vm": vm, "name": checkpoint_name}, vm_name=vm,
        )

    @staticmethod
    def _snapshot_id(snapshot_id: str) -> str:
        value = _text(snapshot_id, "snapshot_id", 64)
        try:
            uuid.UUID(value)
        except ValueError as exc:
            raise ValidationError("snapshot_id must be a valid checkpoint GUID.") from exc
        return value

    def restore_checkpoint(
        self, vm_name: str, snapshot_id: str, confirm_vm: str, confirm_snapshot_id: str
    ) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_checkpoints, "HYPERV_MCP_ALLOW_CHECKPOINTS"
        )
        snapshot = self._snapshot_id(snapshot_id)
        self._confirm(confirm_vm, vm, "confirm_vm")
        self._confirm(confirm_snapshot_id, snapshot, "confirm_snapshot_id")
        return self._run(
            "checkpoint_restore", "checkpoint_restore",
            {"vm": vm, "snapshot_id": snapshot}, vm_name=vm,
        )

    def delete_checkpoint(
        self, vm_name: str, snapshot_id: str, confirm_vm: str, confirm_snapshot_id: str
    ) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_checkpoints, "HYPERV_MCP_ALLOW_CHECKPOINTS"
        )
        snapshot = self._snapshot_id(snapshot_id)
        self._confirm(confirm_vm, vm, "confirm_vm")
        self._confirm(confirm_snapshot_id, snapshot, "confirm_snapshot_id")
        return self._run(
            "checkpoint_delete", "checkpoint_delete",
            {"vm": vm, "snapshot_id": snapshot}, vm_name=vm,
        )

    def _guest(self, vm_name: str) -> dict[str, str]:
        vm = self._write(vm_name, self.settings.allow_guest, "HYPERV_MCP_ALLOW_GUEST")
        if not self.settings.guest_username or not self.settings.guest_password:
            raise PolicyError(
                "PowerShell Direct requires HYPERV_MCP_GUEST_USERNAME and "
                "HYPERV_MCP_GUEST_PASSWORD in the server environment."
            )
        return {
            "vm": vm,
            "username": self.settings.guest_username,
            "password": self.settings.guest_password,
        }

    def guest_exec(self, vm_name: str, command: str) -> dict[str, Any]:
        payload = self._guest(vm_name)
        payload["command"] = _text(command, "command", 131_072)
        payload["max_output_chars"] = self.settings.max_output_chars
        return self._run("guest_exec", "guest_exec", payload, vm_name=vm_name)

    def guest_status(self, vm_name: str) -> dict[str, Any]:
        return self._run(
            "guest_status", "guest_status", self._guest(vm_name), vm_name=vm_name
        )

    def guest_processes(self, vm_name: str, name_contains: str = "") -> dict[str, Any]:
        payload = self._guest(vm_name)
        payload["name_contains"] = _text(
            name_contains, "name_contains", 256, allow_empty=True
        )
        return self._run("guest_processes", "guest_processes", payload, vm_name=vm_name)

    def guest_services(
        self, vm_name: str, name_contains: str = "", include_stopped: bool = False
    ) -> dict[str, Any]:
        payload = self._guest(vm_name)
        payload["name_contains"] = _text(
            name_contains, "name_contains", 256, allow_empty=True
        )
        if not isinstance(include_stopped, bool):
            raise ValidationError("include_stopped must be a boolean value.")
        payload["include_stopped"] = include_stopped
        return self._run("guest_services", "guest_services", payload, vm_name=vm_name)

    def guest_events(
        self,
        vm_name: str,
        log_name: str = "System",
        max_events: int = 30,
        event_id: int | None = None,
        level: int | None = None,
        hours: int = 72,
    ) -> dict[str, Any]:
        payload = self._guest(vm_name)
        if log_name not in {"System", "Application", "Security", "Setup"}:
            raise ValidationError("log_name must be System, Application, Security, or Setup.")
        payload.update(
            log_name=log_name,
            max_events=_integer(max_events, "max_events", 1, 200),
            hours=_integer(hours, "hours", 1, 24 * 365),
        )
        if event_id is not None:
            payload["event_id"] = _integer(event_id, "event_id", 0, 65_535)
        if level is not None:
            payload["level"] = _integer(level, "level", 1, 5)
        return self._run("guest_events", "guest_events", payload, vm_name=vm_name)

    def guest_read_file(
        self, vm_name: str, guest_path: str, max_bytes: int = 1_048_576
    ) -> dict[str, Any]:
        payload = self._guest(vm_name)
        payload["path"] = _text(guest_path, "guest_path", 32_768)
        payload["max_bytes"] = _integer(max_bytes, "max_bytes", 1, MAX_GUEST_FILE_BYTES)
        return self._run("guest_read_file", "guest_read_file", payload, vm_name=vm_name)

    def guest_write_file(
        self,
        vm_name: str,
        guest_path: str,
        content_base64: str,
        *,
        overwrite: bool = False,
        confirm_path: str | None = None,
        create_parent: bool = False,
    ) -> dict[str, Any]:
        payload = self._guest(vm_name)
        path = _text(guest_path, "guest_path", 32_768)
        _text(content_base64, "content_base64", 14_000_000)
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError("content_base64 is not valid base64.") from exc
        if len(content) > MAX_GUEST_FILE_BYTES:
            raise ValidationError("Decoded file content exceeds the 8 MiB limit.")
        if not isinstance(overwrite, bool) or not isinstance(create_parent, bool):
            raise ValidationError("overwrite and create_parent must be boolean values.")
        if overwrite:
            self._confirm(confirm_path or "", path, "confirm_path")
        payload.update(
            path=path,
            content_base64=content_base64,
            overwrite=overwrite,
            create_parent=create_parent,
        )
        return self._run("guest_write_file", "guest_write_file", payload, vm_name=vm_name)

    def console_info(self, vm_name: str) -> dict[str, Any]:
        name = self._vm(vm_name)
        return self._run("console_info", "console_info", {"vm": name}, vm_name=name)

    def console_type_text(self, vm_name: str, text: str) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_console, "HYPERV_MCP_ALLOW_CONSOLE"
        )
        value = _text(text, "text", 16_384, allow_empty=True)
        result = self._run(
            "console_type_text", "console_type_text", {"vm": vm, "text": value}, vm_name=vm
        )
        result["characters_requested"] = len(value)
        return result

    def console_press_keys(self, vm_name: str, keys: list[str]) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_console, "HYPERV_MCP_ALLOW_CONSOLE"
        )
        if not isinstance(keys, list) or not 1 <= len(keys) <= 6:
            raise ValidationError("keys must contain between one and six key names.")
        normalized = [str(key).casefold() for key in keys]
        if any(key not in KEY_NAMES for key in normalized) or len(set(normalized)) != len(normalized):
            raise ValidationError("keys contains an unsupported or duplicate key name.")
        return self._run(
            "console_press_keys", "console_press_keys", {"vm": vm, "keys": normalized}, vm_name=vm
        )

    def console_key_state(self, vm_name: str, key: str, down: bool) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_console, "HYPERV_MCP_ALLOW_CONSOLE"
        )
        normalized = str(key).casefold()
        if normalized not in KEY_NAMES:
            raise ValidationError("key is not a supported key name.")
        if not isinstance(down, bool):
            raise ValidationError("down must be a boolean value.")
        return self._run(
            "console_key_state", "console_key_state",
            {"vm": vm, "key": normalized, "down": down}, vm_name=vm,
        )

    def console_mouse_buttons(
        self,
        vm_name: str,
        left: bool = False,
        right: bool = False,
        middle: bool = False,
    ) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_console, "HYPERV_MCP_ALLOW_CONSOLE"
        )
        if not all(isinstance(value, bool) for value in (left, right, middle)):
            raise ValidationError("Mouse button states must be boolean values.")
        return self._run(
            "console_mouse_buttons", "console_mouse_buttons",
            {"vm": vm, "left": left, "right": right, "middle": middle}, vm_name=vm,
        )

    def console_wait(self, milliseconds: int) -> dict[str, Any]:
        duration = _integer(milliseconds, "milliseconds", 1, 30_000)
        time.sleep(duration / 1000)
        return {"waited_ms": duration}

    def console_batch(self, vm_name: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
        self._vm(vm_name)
        if not isinstance(actions, list) or not 1 <= len(actions) <= 32:
            raise ValidationError("actions must contain between one and 32 console actions.")
        results = []
        allowed_fields = {
            "text": {"text"}, "keys": {"keys"}, "click": {"button", "x", "y"},
            "move": {"x", "y"}, "scroll": {"delta"}, "wait": {"milliseconds"},
            "ctrl_alt_delete": set(),
        }
        for index, action in enumerate(actions):
            if not isinstance(action, dict) or not isinstance(action.get("type"), str):
                raise ValidationError(f"actions[{index}] must have a string type.")
            kind = action["type"]
            if kind not in allowed_fields:
                raise ValidationError(f"Unsupported console action type: {kind!r}.")
            unexpected = set(action) - {"type"} - allowed_fields[kind]
            if unexpected:
                raise ValidationError(f"actions[{index}] contains unsupported fields.")
        for index, action in enumerate(actions):
            kind = action["type"]
            arguments = {key: value for key, value in action.items() if key != "type"}
            if kind == "text":
                result = self.console_type_text(vm_name, arguments.get("text", ""))
            elif kind == "keys":
                result = self.console_press_keys(vm_name, arguments.get("keys", []))
            elif kind == "click":
                result = self.console_click(
                    vm_name, arguments.get("button", "left"),
                    arguments.get("x"), arguments.get("y"),
                )
            elif kind == "move":
                result = self.console_mouse_move(
                    vm_name, arguments.get("x"), arguments.get("y")
                )
            elif kind == "scroll":
                result = self.console_scroll(vm_name, arguments.get("delta"))
            elif kind == "wait":
                result = self.console_wait(arguments.get("milliseconds"))
            elif kind == "ctrl_alt_delete":
                result = self.console_ctrl_alt_delete(vm_name)
            results.append({"index": index, "type": kind, "result": result})
        return {"vm": vm_name, "actions_completed": len(results), "results": results}

    def console_ctrl_alt_delete(self, vm_name: str) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_console, "HYPERV_MCP_ALLOW_CONSOLE"
        )
        return self._run("console_ctrl_alt_delete", "console_ctrl_alt_delete", {"vm": vm}, vm_name=vm)

    def console_mouse_move(self, vm_name: str, x: int, y: int) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_console, "HYPERV_MCP_ALLOW_CONSOLE"
        )
        payload = {
            "vm": vm,
            "x": _integer(x, "x", 0, 32_767),
            "y": _integer(y, "y", 0, 32_767),
        }
        return self._run("console_mouse_move", "console_mouse_move", payload, vm_name=vm)

    def console_click(
        self, vm_name: str, button: str = "left", x: int | None = None, y: int | None = None
    ) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_console, "HYPERV_MCP_ALLOW_CONSOLE"
        )
        if button not in BUTTONS:
            raise ValidationError("button must be left, right, or middle.")
        if (x is None) != (y is None):
            raise ValidationError("x and y must be supplied together.")
        payload: dict[str, Any] = {"vm": vm, "button_index": BUTTONS[button]}
        if x is not None and y is not None:
            payload.update(
                x=_integer(x, "x", 0, 32_767), y=_integer(y, "y", 0, 32_767)
            )
        return self._run("console_click", "console_click", payload, vm_name=vm)

    def console_scroll(self, vm_name: str, delta: int) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_console, "HYPERV_MCP_ALLOW_CONSOLE"
        )
        return self._run(
            "console_scroll", "console_scroll",
            {"vm": vm, "delta": _integer(delta, "delta", -100_000, 100_000)}, vm_name=vm,
        )

    def authorize_screenshot(self, vm_name: str) -> str:
        return self._write(
            vm_name, self.settings.allow_screenshots, "HYPERV_MCP_ALLOW_SCREENSHOTS"
        )

    def debug_serial_status(self, vm_name: str) -> dict[str, Any]:
        name = self._vm(vm_name)
        return self._run(
            "debug_serial_status", "debug_serial_status", {"vm": name}, vm_name=name
        )

    def debug_serial_prepare(
        self,
        vm_name: str,
        pipe_name: str,
        confirm_vm: str,
    ) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_debugger, "HYPERV_MCP_ALLOW_DEBUGGER"
        )
        if not isinstance(pipe_name, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", pipe_name
        ):
            raise ValidationError(
                "pipe_name must contain only letters, digits, dot, underscore, or hyphen."
            )
        self._confirm(confirm_vm, vm, "confirm_vm")
        return self._run(
            "debug_serial_prepare", "debug_serial_prepare",
            {"vm": vm, "pipe_name": pipe_name}, vm_name=vm,
        )

    def debug_guest_prepare(self, vm_name: str, confirm_vm: str) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_debugger, "HYPERV_MCP_ALLOW_DEBUGGER"
        )
        self._confirm(confirm_vm, vm, "confirm_vm")
        if not self.settings.guest_username or not self.settings.guest_password:
            raise PolicyError(
                "Guest debug preparation requires guest credentials in the server environment."
            )
        return self._run(
            "debug_guest_prepare", "debug_guest_prepare",
            {
                "vm": vm,
                "username": self.settings.guest_username,
                "password": self.settings.guest_password,
            },
            vm_name=vm,
        )

    def serial_status(self, vm_name: str) -> dict[str, Any]:
        name = self._vm(vm_name)
        result = self._run("serial_status", "debug_serial_status", {"vm": name}, vm_name=name)
        result["console"] = self.serial.status(name)
        return result

    def serial_connect(
        self, vm_name: str, pipe_name: str, log_path: str, confirm_vm: str
    ) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_debugger, "HYPERV_MCP_ALLOW_DEBUGGER"
        )
        self._confirm(confirm_vm, vm, "confirm_vm")
        name = _text(pipe_name, "pipe_name", 64)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
            raise ValidationError("pipe_name contains unsupported characters.")
        log = self._serial_log(log_path)
        status = self.serial_status(vm)
        expected = rf"\\.\pipe\{name}"
        matching = [
            port for port in status.get("com_ports", [])
            if port.get("number") == 1 and str(port.get("path", "")).casefold() == expected.casefold()
        ]
        if len(matching) != 1:
            raise ValidationError("pipe_name must exactly match the path configured on the VM's COM1 port.")
        if str(status.get("state", "")).casefold() != "running":
            raise ValidationError("The VM must be Running before attaching to its serial pipe.")
        return self._serial_action(
            "serial_connect", vm, lambda: self.serial.connect(vm, name, log)
        )

    def serial_read(self, vm_name: str, tail_chars: int = 32_768) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_debugger, "HYPERV_MCP_ALLOW_DEBUGGER"
        )
        tail = _integer(tail_chars, "tail_chars", 1, 262_144)
        return self._serial_action("serial_read", vm, lambda: self.serial.read(vm, tail))

    def serial_send(
        self, vm_name: str, text: str, append_newline: bool = True
    ) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_debugger, "HYPERV_MCP_ALLOW_DEBUGGER"
        )
        value = _text(text, "text", 32_768, allow_empty=True)
        if not isinstance(append_newline, bool):
            raise ValidationError("append_newline must be a boolean value.")
        data = value + ("\r\n" if append_newline else "")
        return self._serial_action("serial_send", vm, lambda: self.serial.send(vm, data))

    def serial_disconnect(self, vm_name: str) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_debugger, "HYPERV_MCP_ALLOW_DEBUGGER"
        )
        return self._serial_action(
            "serial_disconnect", vm, lambda: self.serial.disconnect(vm)
        )

    @staticmethod
    def _serial_log(path: str) -> str:
        value = _text(path, "log_path", 32_768)
        if not value.lower().endswith(".log"):
            raise ValidationError("log_path must end with .log.")
        import os
        from pathlib import Path

        full = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(value))))
        return str(full)

    def media_list(self, vm_name: str) -> dict[str, Any]:
        name = self._vm(vm_name)
        return self._run("media_list", "media_list", {"vm": name}, vm_name=name)

    def media_mount(
        self,
        vm_name: str,
        iso_path: str,
        controller_location: int,
        confirm_vm: str,
        boot_first: bool = False,
        controller_number: int = 0,
    ) -> dict[str, Any]:
        vm = self._write(vm_name, self.settings.allow_media, "HYPERV_MCP_ALLOW_MEDIA")
        self._confirm(confirm_vm, vm, "confirm_vm")
        path = _text(iso_path, "iso_path", 32_768)
        if not path.lower().endswith(".iso"):
            raise ValidationError("iso_path must name an .iso file.")
        if not isinstance(boot_first, bool):
            raise ValidationError("boot_first must be a boolean value.")
        return self._run(
            "media_mount", "media_mount",
            {"vm": vm, "path": path, "controller_number": _integer(
                controller_number, "controller_number", 0, 1
            ), "controller_location": _integer(
                controller_location, "controller_location", 0, 63
            ), "boot_first": boot_first},
            vm_name=vm,
        )

    def media_eject(
        self, vm_name: str, controller_location: int, confirm_vm: str, controller_number: int = 0
    ) -> dict[str, Any]:
        vm = self._write(vm_name, self.settings.allow_media, "HYPERV_MCP_ALLOW_MEDIA")
        self._confirm(confirm_vm, vm, "confirm_vm")
        return self._run(
            "media_eject", "media_eject",
            {"vm": vm, "controller_number": _integer(
                controller_number, "controller_number", 0, 1
            ), "controller_location": _integer(
                controller_location, "controller_location", 0, 63
            )}, vm_name=vm,
        )
