"""Validation and authorization, independent of the live Hyper-V backend."""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from typing import Any, Callable, TypeVar

from .audit import AuditLogger
from .backend import HyperVBackend
from .config import Settings
from .errors import HyperVError, PolicyError, ValidationError

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
    ) -> None:
        self.backend = backend
        self.settings = settings or Settings()
        self.audit = audit or AuditLogger(self.settings.audit_log)

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
        if action not in {"start", "shutdown", "save", "turn_off", "reset"}:
            raise ValidationError("action must be start, shutdown, save, turn_off, or reset.")
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
        configure_guest: bool = False,
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
        if not isinstance(configure_guest, bool):
            raise ValidationError("configure_guest must be a boolean value.")
        payload: dict[str, Any] = {
            "vm": vm,
            "pipe_name": pipe_name,
            "configure_guest": configure_guest,
        }
        if configure_guest:
            if not self.settings.guest_username or not self.settings.guest_password:
                raise PolicyError(
                    "configure_guest requires guest credentials in the server environment."
                )
            payload.update(
                username=self.settings.guest_username,
                password=self.settings.guest_password,
            )
        return self._run("debug_serial_prepare", "debug_serial_prepare", payload, vm_name=vm)
