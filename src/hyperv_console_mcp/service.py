"""Validation and authorization, independent of the live Hyper-V backend."""

from __future__ import annotations

import base64
import binascii
import os
import re
import time
import uuid
from pathlib import Path
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


def _ascii_keyboard_text(value: str) -> str:
    if any(ord(character) > 126 or (ord(character) < 32 and character not in "\r\n\t")
           for character in value):
        raise ValidationError(
            "text must contain only printable ASCII, tab, CR, or LF for Msvm_Keyboard. "
            "Use guest_exec for Unicode; the guest keyboard layout must also match the intended characters."
        )
    return value


def _confined_path(value: str, label: str, root: Path, suffix: str) -> str:
    text = _text(value, label, 32_768)
    if not text.casefold().endswith(suffix):
        raise ValidationError(f"{label} must end with {suffix}.")
    base = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(str(root))))).resolve()
    full = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(text)))).resolve()
    if full == base or not full.is_relative_to(base):
        raise PolicyError(f"{label} must remain under the configured root: {base}")
    return str(full)


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
        audit: bool = True,
    ) -> dict[str, Any]:
        try:
            result = self.backend.invoke(
                bridge_operation, payload or {}, timeout=self.settings.timeout_seconds
            )
        except HyperVError as exc:
            if audit:
                self.audit.record(operation, vm_name, "error", exc.code)
            raise
        except Exception:
            if audit:
                self.audit.record(operation, vm_name, "error", "unexpected_error")
            raise
        if audit:
            self.audit.record(operation, vm_name, "success")
        return result

    def _local_action(
        self, operation: str, vm_name: str, action: Callable[[], T]
    ) -> T:
        from .events import EVENTS, require_dashboard_approval
        with EVENTS.operation(operation, vm_name):
            if operation in {"serial_connect", "serial_send", "serial_disconnect", "console_screenshot"}:
                require_dashboard_approval(operation, vm_name)
            try:
                result = action()
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
        result = self._local_action(operation, vm_name, action)
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
        from .events import EVENTS
        try:
            with EVENTS.operation("vm_wait", name):
                while True:
                    result = self._run(
                        "vm_get", "vm_get", {"vm": name}, vm_name=name, audit=False
                    )
                    polls += 1
                    current = str(result["vm"]["state"])
                    if current.casefold() in {state.strip().casefold() for state in states}:
                        self.audit.record("vm_wait", name, "success")
                        return {"matched": True, "polls": polls, "vm": result["vm"]}
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.audit.record("vm_wait", name, "success")
                        return {
                            "matched": False, "polls": polls, "vm": result["vm"],
                            "message": "Timed out waiting for the requested state. No power action was taken.",
                        }
                    time.sleep(min(interval / 1000, remaining))
        except HyperVError as exc:
            self.audit.record("vm_wait", name, "error", exc.code)
            raise
        except Exception:
            self.audit.record("vm_wait", name, "error", "unexpected_error")
            raise

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

    def operation_status(self, operation_id: str) -> dict[str, Any]:
        """Track a background power operation started by shutdown or save."""
        identifier = _text(operation_id, "operation_id", 64)
        if re.fullmatch(r"[0-9a-fA-F]{32}", identifier) is None:
            raise ValidationError("operation_id must be the 32-character identifier returned by vm_power.")
        return self._run(
            "operation_status", "operation_status", {"operation_id": identifier.lower()}
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
        self, vm_name: str, guest_path: str, max_bytes: int = 65_536
    ) -> dict[str, Any]:
        payload = self._guest(vm_name)
        payload["path"] = _text(guest_path, "guest_path", 32_768)
        payload["max_bytes"] = _integer(max_bytes, "max_bytes", 1, MAX_GUEST_FILE_BYTES)
        return self._run("guest_read_file", "guest_read_file", payload, vm_name=vm_name)

    def guest_read_chunk(
        self, vm_name: str, guest_path: str, offset: int, count: int
    ) -> dict[str, Any]:
        payload = self._guest(vm_name)
        payload["path"] = _text(guest_path, "guest_path", 32_768)
        payload["offset"] = _integer(offset, "offset", 0, MAX_GUEST_FILE_BYTES * 256)
        payload["count"] = _integer(count, "count", 1, MAX_GUEST_FILE_BYTES)
        return self._run("guest_read_chunk", "guest_read_chunk", payload, vm_name=vm_name)

    def guest_assemble_file(
        self,
        vm_name: str,
        guest_path: str,
        parts_directory: str,
        part_count: int,
        expected_size: int,
        expected_sha256: str,
    ) -> dict[str, Any]:
        payload = self._guest(vm_name)
        payload.update(
            path=_text(guest_path, "guest_path", 32_768),
            parts_directory=_text(parts_directory, "parts_directory", 32_768),
            part_count=_integer(part_count, "part_count", 1, 2048),
            expected_size=_integer(expected_size, "expected_size", 0, MAX_GUEST_FILE_BYTES * 256),
            expected_sha256=_text(expected_sha256, "expected_sha256", 64),
        )
        if not re.fullmatch(r"[0-9a-f]{64}", payload["expected_sha256"]):
            raise ValidationError("expected_sha256 must be a lowercase SHA-256 hexadecimal string.")
        return self._run("guest_assemble_file", "guest_assemble_file", payload, vm_name=vm_name)

    def guest_publish_package(
        self, vm_name: str, package_id: str, transfer_id: str, manifest_sha256: str
    ) -> dict[str, Any]:
        """Verify a staged package inside the guest and move it into Packages atomically."""
        payload = self._guest(vm_name)
        payload.update(
            package_id=_text(package_id, "package_id", 64),
            transfer_id=_text(transfer_id, "transfer_id", 32),
            manifest_sha256=_text(manifest_sha256, "manifest_sha256", 64),
        )
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}", payload["package_id"]) is None:
            raise ValidationError("package_id must be 1-64 letters, digits, dots, or hyphens.")
        if re.fullmatch(r"[0-9a-f]{32}", payload["transfer_id"]) is None:
            raise ValidationError("transfer_id must be a 32-character lowercase hexadecimal id.")
        if re.fullmatch(r"[0-9a-f]{64}", payload["manifest_sha256"]) is None:
            raise ValidationError("manifest_sha256 must be a lowercase SHA-256 hexadecimal string.")
        return self._run("guest_publish_package", "guest_publish_package", payload, vm_name=vm_name)

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
        _text(content_base64, "content_base64", 14_000_000, allow_empty=True)
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

    def lab_init(self, vm_name: str, confirm_vm: str) -> dict[str, Any]:
        from .lab.cli import initialize
        name = self._guest(vm_name)["vm"]
        self._confirm(confirm_vm, name, "confirm_vm")
        return initialize(self, name)

    def lab_diagnostics(self, vm_name: str) -> dict[str, Any]:
        from .lab.cli import diagnostics
        return diagnostics(self, vm_name)

    def lab_stage(self, vm_name: str, source_directory: str, package_id: str | None = None) -> dict[str, Any]:
        from pathlib import Path

        from .lab.transfer import DriverLabTransfer, build_manifest
        self._guest(vm_name)
        root = self.settings.lab_root.expanduser().resolve()
        source = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(
            _text(source_directory, "source_directory", 32_768)
        )))).resolve()
        if not source.is_relative_to(root) or source == root:
            raise PolicyError(
                f"The lab source must be a subdirectory of HYPERV_MCP_LAB_ROOT ({root})."
            )
        manifest = build_manifest(source, package_id)
        return DriverLabTransfer(self).stage(vm_name, source, manifest)

    def lab_verify(self, vm_name: str, package_id: str, expected_manifest_sha256: str) -> dict[str, Any]:
        from .lab.transfer import DriverLabTransfer
        return DriverLabTransfer(self).verify(vm_name, package_id, expected_manifest_sha256)

    def lab_collect(
        self, vm_name: str, package_id: str, run_id: str,
        include_kernel_dump: bool = False,
    ) -> dict[str, Any]:
        from .lab.transfer import DriverLabTransfer
        output = self.settings.evidence_root.expanduser().resolve()
        return DriverLabTransfer(self).collect(
            vm_name, package_id, run_id, output, include_kernel_dump=include_kernel_dump,
        )

    def console_info(self, vm_name: str) -> dict[str, Any]:
        name = self._vm(vm_name)
        return self._run("console_info", "console_info", {"vm": name}, vm_name=name)

    def console_type_text(self, vm_name: str, text: str) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_console, "HYPERV_MCP_ALLOW_CONSOLE"
        )
        value = _ascii_keyboard_text(_text(text, "text", 16_384, allow_empty=True))
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
        if any(not isinstance(key, str) for key in keys):
            raise ValidationError("keys must contain only string key names.")
        normalized = [key.casefold() for key in keys]
        if any(key not in KEY_NAMES for key in normalized) or len(set(normalized)) != len(normalized):
            raise ValidationError("keys contains an unsupported or duplicate key name.")
        return self._run(
            "console_press_keys", "console_press_keys", {"vm": vm, "keys": normalized}, vm_name=vm
        )

    def console_key_state(self, vm_name: str, key: str, down: bool) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_console, "HYPERV_MCP_ALLOW_CONSOLE"
        )
        if not isinstance(key, str):
            raise ValidationError("key must be a string key name.")
        normalized = key.casefold()
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
        prepared: list[tuple[int, str, dict[str, Any]]] = []
        wait_budget = 0
        for index, action in enumerate(actions):
            kind = action["type"]
            arguments = {key: value for key, value in action.items() if key != "type"}
            if kind == "text":
                arguments["text"] = _ascii_keyboard_text(_text(
                    arguments.get("text", ""), f"actions[{index}].text", 16_384, allow_empty=True
                ))
            elif kind == "keys":
                keys = arguments.get("keys", [])
                if not isinstance(keys, list) or not 1 <= len(keys) <= 6 or any(
                    not isinstance(key, str) for key in keys
                ):
                    raise ValidationError(f"actions[{index}].keys must contain one to six strings.")
                normalized = [key.casefold() for key in keys]
                if any(key not in KEY_NAMES for key in normalized) or len(set(normalized)) != len(normalized):
                    raise ValidationError(f"actions[{index}].keys contains an unsupported or duplicate key.")
                arguments["keys"] = normalized
            elif kind in {"move", "click"}:
                x, y = arguments.get("x"), arguments.get("y")
                if kind == "move" or x is not None or y is not None:
                    if (x is None) != (y is None):
                        raise ValidationError(f"actions[{index}] x and y must be supplied together.")
                    arguments["x"] = _integer(x, f"actions[{index}].x", 0, 32_767)
                    arguments["y"] = _integer(y, f"actions[{index}].y", 0, 32_767)
                if kind == "click":
                    button = arguments.get("button", "left")
                    if button not in BUTTONS:
                        raise ValidationError(f"actions[{index}].button must be left, right, or middle.")
                    arguments["button"] = button
            elif kind == "scroll":
                arguments["delta"] = _integer(
                    arguments.get("delta"), f"actions[{index}].delta", -100_000, 100_000
                )
            elif kind == "wait":
                duration = _integer(
                    arguments.get("milliseconds"), f"actions[{index}].milliseconds", 1, 30_000
                )
                wait_budget += duration
                if wait_budget > 30_000:
                    raise ValidationError("The cumulative console batch wait budget is 30000 ms.")
                arguments["milliseconds"] = duration
            prepared.append((index, kind, arguments))
        results = []
        for index, kind, arguments in prepared:
            if kind == "text":
                result = self.console_type_text(vm_name, arguments["text"])
            elif kind == "keys":
                result = self.console_press_keys(vm_name, arguments["keys"])
            elif kind == "click":
                result = self.console_click(
                    vm_name, arguments.get("button", "left"), arguments.get("x"), arguments.get("y")
                )
            elif kind == "move":
                result = self.console_mouse_move(vm_name, arguments["x"], arguments["y"])
            elif kind == "scroll":
                result = self.console_scroll(vm_name, arguments["delta"])
            elif kind == "wait":
                result = self.console_wait(arguments["milliseconds"])
            else:
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

    def capture_screenshot(
        self, capture: Any, vm_name: str, region: str = "window", max_width: int = 1600
    ) -> Any:
        name = self.authorize_screenshot(vm_name)
        return self._local_action(
            "console_screenshot", name,
            lambda: capture.capture(name, region=region, max_width=max_width),
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

    def _serial_log(self, path: str) -> str:
        return _confined_path(path, "log_path", self.settings.serial_log_root, ".log")

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
        path = _confined_path(iso_path, "iso_path", self.settings.iso_root, ".iso")
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
