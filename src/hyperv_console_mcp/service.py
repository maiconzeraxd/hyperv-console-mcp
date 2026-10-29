"""Validation and authorization, independent of the live Hyper-V backend."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import time
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, TypeVar

from .audit import AuditLogger
from .backend import HyperVBackend
from .config import Settings
from .errors import BackendError, HyperVError, PolicyError, ValidationError
from .serial import SerialManager

MAX_GUEST_FILE_BYTES = 8 * 1024 * 1024
COMMAND_PREVIEW_LINES = 20
# Every MCP tool annotated MUTATION must request approval here, once per call.
GATED_TOOLS = frozenset({
    "vm_power", "checkpoint_create", "checkpoint_restore", "checkpoint_delete",
    "guest_exec", "guest_write_file", "guest_put_file", "guest_get_file",
    "guest_ssh_provision", "guest_ssh_deprovision",
    "guest_password_rotate", "guest_credential_forget",
    "guest_autologon_configure", "guest_config_revert",
    "lab_init", "lab_stage", "lab_collect",
    "console_type_text", "console_press_keys", "console_key_state",
    "console_mouse_buttons", "console_batch", "console_ctrl_alt_delete",
    "console_mouse_move", "console_click", "console_scroll",
    "media_mount", "media_eject",
    "serial_connect", "serial_send", "serial_disconnect",
    "debug_serial_prepare", "debug_guest_prepare",
})
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


def _command_summary(command: str) -> dict[str, Any]:
    """Informed, secret-free preview of a guest command for the approval dialog."""
    lines = command.splitlines() or [""]
    return {
        "command_preview": "\n".join(lines[:COMMAND_PREVIEW_LINES]),
        "command_lines": len(lines),
        "command_truncated": len(lines) > COMMAND_PREVIEW_LINES,
        "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
    }


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

    def _call(self, operation: str, vm_name: str | None, action: Callable[[], T]) -> T:
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

    def _run(
        self,
        operation: str,
        bridge_operation: str,
        payload: dict[str, Any] | None = None,
        *,
        vm_name: str | None = None,
        audit: bool = True,
    ) -> dict[str, Any]:
        def invoke() -> dict[str, Any]:
            return self.backend.invoke(
                bridge_operation, payload or {}, timeout=self.settings.timeout_seconds
            )

        if audit:
            return self._call(operation, vm_name, invoke)
        return invoke()

    def _approve(
        self, operation: str, vm_name: str, summary: dict[str, Any] | None = None
    ) -> AbstractContextManager[None]:
        """One dashboard approval per tool call; nested mutations inherit it."""
        from .events import approval_scope
        return approval_scope(operation, vm_name, summary)

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
        result = self._run("host_status", "host_status")
        result["profile"] = self.settings.profile
        result["capabilities"] = {
            "power": self.settings.allow_power,
            "checkpoints": self.settings.allow_checkpoints,
            "guest": self.settings.allow_guest,
            "console": self.settings.allow_console,
            "screenshots": self.settings.allow_screenshots,
            "debugger": self.settings.allow_debugger,
            "media": self.settings.allow_media,
        }
        result["allowlist"] = sorted(self.settings.allowed_vms)
        return result

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
        with self._approve("vm_power", name, {
            "action": action, "forced": action in {"turn_off", "reset"},
        }):
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
        with self._approve("checkpoint_create", vm, {"checkpoint_name": checkpoint_name}):
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
        with self._approve("checkpoint_restore", vm, {"snapshot_id": snapshot}):
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
        with self._approve("checkpoint_delete", vm, {"snapshot_id": snapshot}):
            return self._run(
                "checkpoint_delete", "checkpoint_delete",
                {"vm": vm, "snapshot_id": snapshot}, vm_name=vm,
            )

    def _guest_vm(self, vm_name: str) -> str:
        return self._write(vm_name, self.settings.allow_guest, "HYPERV_MCP_ALLOW_GUEST")

    def _transport(self, vm_name: str) -> Any:
        from .transport import select_transport
        return select_transport(self.settings, self.backend, vm_name)

    def _guest_call(
        self,
        operation: str,
        vm_name: str,
        payload: dict[str, Any] | None = None,
        *,
        summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one structured guest operation through the configured transport."""
        name = self._guest_vm(vm_name)
        transport = self._transport(name)
        request = dict(payload or {})

        def run() -> dict[str, Any]:
            return transport.call(name, operation, request)

        if summary is None:
            result = self._call(operation, name, run)
        else:
            with self._approve(operation, name, summary):
                result = self._call(operation, name, run)
        return {**result, "vm": result.get("vm", name), "transport": transport.name}

    def guest_exec(self, vm_name: str, command: str) -> dict[str, Any]:
        text = _text(command, "command", 131_072)
        name = self._guest_vm(vm_name)
        transport = self._transport(name)
        with self._approve("guest_exec", name, _command_summary(text)):
            execution = self._call(
                "guest_exec",
                name,
                lambda: transport.exec(
                    name,
                    text,
                    timeout=self.settings.timeout_seconds,
                    max_output=self.settings.max_output_chars,
                ),
            )
        return {"vm": name, "execution": execution.as_dict()}

    def guest_status(self, vm_name: str) -> dict[str, Any]:
        name = self._guest_vm(vm_name)
        transport = self._transport(name)
        guest = self._call("guest_status", name, lambda: transport.health(name))
        return {"vm": name, "transport": transport.name, "guest": guest}

    def guest_processes(self, vm_name: str, name_contains: str = "") -> dict[str, Any]:
        return self._guest_call(
            "guest_processes",
            vm_name,
            {"name_contains": _text(name_contains, "name_contains", 256, allow_empty=True)},
        )

    def guest_services(
        self, vm_name: str, name_contains: str = "", include_stopped: bool = False
    ) -> dict[str, Any]:
        if not isinstance(include_stopped, bool):
            raise ValidationError("include_stopped must be a boolean value.")
        return self._guest_call(
            "guest_services",
            vm_name,
            {
                "name_contains": _text(name_contains, "name_contains", 256, allow_empty=True),
                "include_stopped": include_stopped,
            },
        )

    def guest_events(
        self,
        vm_name: str,
        log_name: str = "System",
        max_events: int = 30,
        event_id: int | None = None,
        level: int | None = None,
        hours: int = 72,
    ) -> dict[str, Any]:
        if log_name not in {"System", "Application", "Security", "Setup"}:
            raise ValidationError("log_name must be System, Application, Security, or Setup.")
        payload: dict[str, Any] = {
            "log_name": log_name,
            "max_events": _integer(max_events, "max_events", 1, 200),
            "hours": _integer(hours, "hours", 1, 24 * 365),
        }
        if event_id is not None:
            payload["event_id"] = _integer(event_id, "event_id", 0, 65_535)
        if level is not None:
            payload["level"] = _integer(level, "level", 1, 5)
        return self._guest_call("guest_events", vm_name, payload)

    def guest_read_file(
        self, vm_name: str, guest_path: str, max_bytes: int = 65_536
    ) -> dict[str, Any]:
        return self._guest_call(
            "guest_read_file",
            vm_name,
            {
                "path": _text(guest_path, "guest_path", 32_768),
                "max_bytes": _integer(max_bytes, "max_bytes", 1, MAX_GUEST_FILE_BYTES),
            },
        )

    def guest_read_chunk(
        self, vm_name: str, guest_path: str, offset: int, count: int
    ) -> dict[str, Any]:
        return self._guest_call(
            "guest_read_chunk",
            vm_name,
            {
                "path": _text(guest_path, "guest_path", 32_768),
                "offset": _integer(offset, "offset", 0, MAX_GUEST_FILE_BYTES * 256),
                "count": _integer(count, "count", 1, MAX_GUEST_FILE_BYTES),
            },
        )

    def guest_assemble_file(
        self,
        vm_name: str,
        guest_path: str,
        parts_directory: str,
        part_count: int,
        expected_size: int,
        expected_sha256: str,
    ) -> dict[str, Any]:
        digest = _text(expected_sha256, "expected_sha256", 64)
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValidationError("expected_sha256 must be a lowercase SHA-256 hexadecimal string.")
        return self._guest_call(
            "guest_assemble_file",
            vm_name,
            {
                "path": _text(guest_path, "guest_path", 32_768),
                "parts_directory": _text(parts_directory, "parts_directory", 32_768),
                "part_count": _integer(part_count, "part_count", 1, 2048),
                "expected_size": _integer(
                    expected_size, "expected_size", 0, MAX_GUEST_FILE_BYTES * 256
                ),
                "expected_sha256": digest,
            },
        )

    def guest_publish_package(
        self, vm_name: str, package_id: str, transfer_id: str, manifest_sha256: str
    ) -> dict[str, Any]:
        """Verify a staged package inside the guest and move it into Packages atomically."""
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}", package_id) is None:
            raise ValidationError("package_id must be 1-64 letters, digits, dots, or hyphens.")
        if re.fullmatch(r"[0-9a-f]{32}", transfer_id) is None:
            raise ValidationError("transfer_id must be a 32-character lowercase hexadecimal id.")
        if re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
            raise ValidationError("manifest_sha256 must be a lowercase SHA-256 hexadecimal string.")
        return self._guest_call(
            "guest_publish_package",
            vm_name,
            {
                "package_id": package_id,
                "transfer_id": transfer_id,
                "manifest_sha256": manifest_sha256,
            },
        )

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
        return self._guest_call(
            "guest_write_file",
            vm_name,
            {
                "path": path,
                "content_base64": content_base64,
                "overwrite": overwrite,
                "create_parent": create_parent,
            },
            summary={
                "path": path, "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(), "overwrite": overwrite,
            },
        )

    def guest_put_file(
        self,
        vm_name: str,
        local_path: str,
        guest_path: str,
        *,
        overwrite: bool = False,
        confirm_path: str | None = None,
    ) -> dict[str, Any]:
        """Copy a host file under HYPERV_MCP_TRANSFER_ROOT into the guest."""
        if not isinstance(overwrite, bool):
            raise ValidationError("overwrite must be a boolean value.")
        source = Path(_confined_path(local_path, "local_path", self.settings.transfer_root, ""))
        if not source.is_file():
            raise ValidationError(f"local_path is not a file: {source}")
        remote = _text(guest_path, "guest_path", 32_768)
        if overwrite:
            self._confirm(confirm_path or "", remote, "confirm_path")
        data = source.read_bytes()
        name = self._guest_vm(vm_name)
        transport = self._transport(name)
        with self._approve(
            "guest_put_file",
            name,
            {
                "local_path": str(source), "guest_path": remote,
                "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "overwrite": overwrite,
            },
        ):
            written = self._call(
                "guest_put_file",
                name,
                lambda: transport.put_file(name, source, remote, overwrite=overwrite),
            )
        return {"vm": name, "file": written.as_dict()}

    def guest_get_file(
        self,
        vm_name: str,
        guest_path: str,
        local_path: str,
        *,
        overwrite: bool = False,
        confirm_path: str | None = None,
    ) -> dict[str, Any]:
        """Copy a guest file to a host path under HYPERV_MCP_TRANSFER_ROOT."""
        if not isinstance(overwrite, bool):
            raise ValidationError("overwrite must be a boolean value.")
        remote = _text(guest_path, "guest_path", 32_768)
        target = Path(_confined_path(local_path, "local_path", self.settings.transfer_root, ""))
        if target.exists() and not overwrite:
            raise ValidationError(
                f"local_path already exists: {target}. Pass overwrite=true and confirm_path."
            )
        if overwrite:
            self._confirm(confirm_path or "", str(target), "confirm_path")
        name = self._guest_vm(vm_name)
        transport = self._transport(name)
        with self._approve(
            "guest_get_file", name, {"guest_path": remote, "local_path": str(target)}
        ):
            fetched = self._call(
                "guest_get_file",
                name,
                lambda: transport.get_file(name, remote, target),
            )
        return {"vm": name, "file": fetched.as_dict()}

    def guest_ssh_provision(
        self, vm_name: str, confirm_vm: str, *, use_existing_adapter: bool = False
    ) -> dict[str, Any]:
        """Create key-only SSH in the guest and pin its host key on the host."""
        from .transport import ssh_setup

        if not isinstance(use_existing_adapter, bool):
            raise ValidationError("use_existing_adapter must be a boolean value.")
        name = self._guest_vm(vm_name)
        self._confirm(confirm_vm, name, "confirm_vm")
        _, host_ip = ssh_setup.host_address(self.settings)
        summary = {
            "vm": name,
            "switch": ssh_setup.SWITCH_NAME,
            "host_ip": host_ip,
            "guest_ip": ssh_setup.guest_address(self.settings, name),
            "installs_sshd": True,
            "password_authentication": False,
            "firewall": f"tcp/22 from {host_ip}",
            "use_existing_adapter": use_existing_adapter,
        }
        with self._approve("guest_ssh_provision", name, summary):
            return self._call(
                "guest_ssh_provision",
                name,
                lambda: ssh_setup.provision(
                    self, name, use_existing_adapter=use_existing_adapter
                ),
            )

    def guest_ssh_status(self, vm_name: str) -> dict[str, Any]:
        from .transport import ssh_setup

        name = self._guest_vm(vm_name)
        return self._call("guest_ssh_status", name, lambda: ssh_setup.status(self, name))

    def guest_ssh_deprovision(self, vm_name: str, confirm_vm: str) -> dict[str, Any]:
        from .transport import ssh_setup

        name = self._guest_vm(vm_name)
        self._confirm(confirm_vm, name, "confirm_vm")
        summary = {
            "vm": name,
            "removes": [
                "firewall_rule", "authorized_key", "pinned_host_key",
                "state_file", "lab_adapter_if_created", "lab_host_address_if_created",
            ],
        }
        with self._approve("guest_ssh_deprovision", name, summary):
            return self._call(
                "guest_ssh_deprovision", name, lambda: ssh_setup.deprovision(self, name)
            )

    def guest_credential_status(self, vm_name: str) -> dict[str, Any]:
        """Where the guest credential comes from, and whether it still works."""
        from . import credentials

        name = self._guest_vm(vm_name)
        origin = credentials.source(self.settings, name)
        entry = None
        try:
            entry = credentials.CredentialStore().read(name)
        except HyperVError:
            entry = None
        username = (entry[0] if entry else None) or self.settings.guest_username
        access: dict[str, Any] = {"psdirect": None, "ssh": None}
        if origin != "missing":
            try:
                self._transport(name).health(name)
                access["psdirect"] = {"ok": True, "error": None}
            except Exception as exc:
                access["psdirect"] = {"ok": False, "error": str(exc)}
            from .transport.ssh import SshTransport
            ssh = SshTransport(self.settings)
            access["ssh"] = (
                {"provisioned": True, **self._safe_health(ssh, name)}
                if ssh.is_provisioned(name) else {"provisioned": False}
            )
        return {
            "vm": name,
            "source": origin,
            "username": username,
            "psdirect": access["psdirect"],
            "ssh": access["ssh"],
        }

    @staticmethod
    def _safe_health(transport: Any, name: str) -> dict[str, Any]:
        try:
            return {"ok": True, "health": transport.health(name)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def guest_credential_set(self, vm_name: str, username: str, password: str) -> dict[str, Any]:
        """Store a credential the operator typed locally. Never an MCP argument."""
        from . import credentials

        name = self._guest_vm(vm_name)
        account = _text(username, "username", 256)
        secret = _text(password, "password", 4096)
        configured = self.settings.guest_username
        if configured and account.casefold() != configured.casefold():
            raise ValidationError(
                f"username must match HYPERV_MCP_GUEST_USERNAME ({configured})."
            )
        credentials.CredentialStore().write(name, account, secret)
        self.audit.record("guest_credential_set", name, "success")
        return {"vm": name, "source": "credential_manager", "username": account}

    def guest_credential_forget(self, vm_name: str, confirm_vm: str) -> dict[str, Any]:
        from . import credentials

        name = self._guest_vm(vm_name)
        self._confirm(confirm_vm, name, "confirm_vm")
        with self._approve("guest_credential_forget", name, {"vm": name, "removes": "vault_entry"}):
            removed = credentials.CredentialStore().delete(name)
            self.audit.record("guest_credential_forget", name, "success")
        return {
            "vm": name, "removed": removed,
            "fallback": credentials.source(self.settings, name),
        }

    def guest_password_rotate(self, vm_name: str, confirm_vm: str) -> dict[str, Any]:
        """Set a new random guest password, prove it works, then store it."""
        from . import credentials

        name = self._guest_vm(vm_name)
        self._confirm(confirm_vm, name, "confirm_vm")
        previous_source = credentials.source(self.settings, name)
        if previous_source == "missing":
            raise PolicyError(
                f"No guest credential for {name} to rotate. Set one first with "
                "guest_credential_set or HYPERV_MCP_GUEST_USERNAME/PASSWORD."
            )
        username, previous_password = credentials.resolve(self.settings, name)
        summary = {
            "vm": name,
            "account": username,
            "stores_in": "Windows Credential Manager",
            "validates_before_saving": True,
            "previous_source": previous_source,
        }
        with self._approve("guest_password_rotate", name, summary):
            return self._call(
                "guest_password_rotate", name,
                lambda: self._rotate(name, username, previous_password, previous_source),
            )

    def _rotate(
        self, name: str, username: str, previous_password: str, previous_source: str
    ) -> dict[str, Any]:
        from . import credentials

        transport = self._transport(name)
        transport.health(name)  # Fail before changing anything if the guest is unreachable.
        new_password = credentials.generate_password()
        self._run(
            "guest_password_rotate", "guest_set_password",
            {"vm": name, "username": username, "password": previous_password,
             "new_password": new_password},
            vm_name=name, audit=False,
        )
        check = self._run(
            "guest_password_rotate", "guest_auth_check",
            {"vm": name, "username": username, "password": new_password},
            vm_name=name, audit=False,
        )
        if not check.get("ok"):
            # Put the old secret back so the guest is not left with a password
            # nobody stored.
            try:
                self._run(
                    "guest_password_rotate", "guest_set_password",
                    {"vm": name, "username": username, "password": new_password,
                     "new_password": previous_password},
                    vm_name=name, audit=False,
                )
                reverted = True
            except Exception:
                reverted = False
            raise BackendError(
                "The guest rejected the new password, so it was "
                + ("restored to the previous one." if reverted else "not restored.")
                + f" Details: {check.get('message')}",
                code="password_rotation_failed",
            )
        credentials.CredentialStore().write(name, username, new_password)
        return {
            "vm": name,
            "account": username,
            "source": "credential_manager",
            "previous_source": previous_source,
            "verified": True,
        }

    def guest_autologon_configure(
        self,
        vm_name: str,
        enabled: bool | None,
        confirm_vm: str,
        *,
        restart: bool = False,
        lock_screen: bool | None = None,
        disable_cad: bool | None = None,
        power: bool | None = None,
        uac: bool | None = None,
        windows_update: bool | None = None,
    ) -> dict[str, Any]:
        """Boot straight to the desktop, with every change recorded and revertible.

        ``enabled`` is tri-state like the other settings: ``None`` leaves
        autologon alone, which is what the dashboard sends when the operator
        only wants to change a toggle.
        """
        from . import credentials
        from .transport import guest_config

        name = self._guest_vm(vm_name)
        self._confirm(confirm_vm, name, "confirm_vm")
        if enabled is not None and not isinstance(enabled, bool):
            raise ValidationError("enabled must be true, false, or omitted.")
        if not isinstance(restart, bool):
            raise ValidationError("restart must be a boolean value.")
        toggles = {
            "lock_screen": lock_screen,
            "disable_cad": disable_cad,
            "power": power,
            "uac": uac,
            "windows_update": windows_update,
        }
        for label, value in toggles.items():
            if value is not None and not isinstance(value, bool):
                raise ValidationError(f"{label} must be true, false, or omitted.")
        if restart:
            self._write(name, self.settings.allow_power, "HYPERV_MCP_ALLOW_POWER")

        account: str | None = None
        document: dict[str, Any] | None
        if enabled is None:
            document = None
        elif enabled:
            username, password = credentials.resolve(self.settings, name)
            if not password:
                raise PolicyError(
                    "Autologon needs the guest password. A blank password is not offered "
                    "because Windows restricts passwordless accounts to console logon, "
                    "which breaks PowerShell Direct and SSH."
                )
            domain = "."
            if "\\" in username:
                domain, username = username.split("\\", 1)
            account = username
            document = {
                "enabled": True, "username": username, "domain": domain, "secret": password,
            }
        else:
            document = {"enabled": False}

        summary = {
            "vm": name,
            "autologon": {"enabled": enabled, "account": account,
                          "secret": "LSA private data" if enabled else None},
            "toggles": {label: value for label, value in toggles.items() if value is not None},
            "state_file": guest_config.GUEST_STATE_PATH,
            "restart": restart,
        }
        with self._approve("guest_autologon_configure", name, summary):
            result = self._call(
                "guest_autologon_configure", name,
                lambda: guest_config.apply(
                    self, name, autologon=document,
                    lock_screen=toggles["lock_screen"],
                    disable_cad=toggles["disable_cad"],
                    power=toggles["power"],
                    uac=toggles["uac"],
                    windows_update=toggles["windows_update"],
                ),
            )
            if restart:
                result = {**result, "restart": self._restart_and_verify(name)}
        return result

    def guest_config_status(self, vm_name: str) -> dict[str, Any]:
        from .transport import guest_config

        name = self._guest_vm(vm_name)
        return self._call("guest_config_status", name, lambda: guest_config.status(self, name))

    def guest_config_revert(self, vm_name: str, confirm_vm: str, *, restart: bool = False) -> dict[str, Any]:
        """Put every recorded setting back the way it was before this tool touched it."""
        from .transport import guest_config

        name = self._guest_vm(vm_name)
        self._confirm(confirm_vm, name, "confirm_vm")
        if not isinstance(restart, bool):
            raise ValidationError("restart must be a boolean value.")
        if restart:
            self._write(name, self.settings.allow_power, "HYPERV_MCP_ALLOW_POWER")
        summary = {
            "vm": name,
            "restores": "every recorded previous value",
            "removes": ["LSA DefaultPassword if it was absent", "guest-config.json"],
            "restart": restart,
        }
        with self._approve("guest_config_revert", name, summary):
            result = self._call(
                "guest_config_revert", name, lambda: guest_config.revert(self, name)
            )
            if restart:
                result = {**result, "restart": self._restart_and_verify(name)}
        return result

    def _restart_and_verify(self, name: str) -> dict[str, Any]:
        """Restart only when asked, then prove an interactive session came back."""
        from .transport import select_transport

        self._run(
            "guest_autologon_configure", "vm_power", {"vm": name, "action": "shutdown"},
            vm_name=name, audit=False,
        )
        stopped = self.vm_wait(name, ["Off"], timeout_seconds=240)
        if not stopped.get("matched"):
            raise BackendError(
                "The VM did not shut down, so it was not restarted. No further power "
                "action was taken.",
                code="restart_timeout",
            )
        self._run(
            "guest_autologon_configure", "vm_power", {"vm": name, "action": "start"},
            vm_name=name, audit=False,
        )
        started = self.vm_wait(name, ["Running"], timeout_seconds=240)
        if not started.get("matched"):
            raise BackendError("The VM did not start again.", code="restart_timeout")
        transport = select_transport(self.settings, self.backend, name)
        return {"restarted": True, "interactive_session": self._await_session(transport, name)}

    @staticmethod
    def _await_session(transport: Any, name: str, timeout_seconds: int = 180) -> dict[str, Any]:
        """Wait for an interactive logon, which is what autologon is supposed to produce."""
        deadline = time.monotonic() + timeout_seconds
        last: str | None = None
        while time.monotonic() < deadline:
            try:
                result = transport.exec(name, "query user", timeout=30, max_output=8192)
                lines = [line.strip() for line in result.output.splitlines() if line.strip()]
                sessions = [
                    line for line in lines if not line.upper().startswith("USERNAME")
                ]
                if sessions:
                    return {"ok": True, "sessions": sessions}
            except Exception as exc:
                last = str(exc)
            time.sleep(2)
        return {
            "ok": False,
            "error": last or "no interactive session appeared before the timeout",
        }

    def lab_init(self, vm_name: str, confirm_vm: str) -> dict[str, Any]:
        from .lab.cli import initialize
        name = self._guest_vm(vm_name)
        self._confirm(confirm_vm, name, "confirm_vm")
        with self._approve("lab_init", name, {"guest_root": r"C:\DriverLab", "installs_tools": True}):
            return initialize(self, name)

    def lab_diagnostics(self, vm_name: str) -> dict[str, Any]:
        from .lab.cli import diagnostics
        return diagnostics(self, vm_name)

    def lab_stage(self, vm_name: str, source_directory: str, package_id: str | None = None) -> dict[str, Any]:
        from pathlib import Path

        from .lab.transfer import DriverLabTransfer, build_manifest, manifest_hash
        name = self._guest_vm(vm_name)
        root = self.settings.lab_root.expanduser().resolve()
        source = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(
            _text(source_directory, "source_directory", 32_768)
        )))).resolve()
        if not source.is_relative_to(root) or source == root:
            raise PolicyError(
                f"The lab source must be a subdirectory of HYPERV_MCP_LAB_ROOT ({root})."
            )
        manifest = build_manifest(source, package_id)
        summary = {
            "package_id": manifest["package_id"],
            "source": str(source),
            "files": len(manifest["files"]),
            "bytes": sum(int(entry["size"]) for entry in manifest["files"]),
            "manifest_sha256": manifest_hash(manifest),
        }
        with self._approve("lab_stage", name, summary):
            return DriverLabTransfer(self).stage(name, source, manifest)

    def lab_verify(self, vm_name: str, package_id: str, expected_manifest_sha256: str) -> dict[str, Any]:
        from .lab.transfer import DriverLabTransfer
        return DriverLabTransfer(self).verify(vm_name, package_id, expected_manifest_sha256)

    def lab_collect(
        self, vm_name: str, package_id: str, run_id: str,
        include_kernel_dump: bool = False,
    ) -> dict[str, Any]:
        from .lab.transfer import DriverLabTransfer
        name = self._guest_vm(vm_name)
        output = self.settings.evidence_root.expanduser().resolve()
        summary = {
            "package_id": package_id, "run_id": run_id,
            "output_root": str(output), "include_kernel_dump": bool(include_kernel_dump),
        }
        with self._approve("lab_collect", name, summary):
            return DriverLabTransfer(self).collect(
                name, package_id, run_id, output, include_kernel_dump=include_kernel_dump,
            )

    def console_info(self, vm_name: str) -> dict[str, Any]:
        name = self._vm(vm_name)
        return self._run("console_info", "console_info", {"vm": name}, vm_name=name)

    def console_type_text(self, vm_name: str, text: str) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_console, "HYPERV_MCP_ALLOW_CONSOLE"
        )
        value = _ascii_keyboard_text(_text(text, "text", 16_384, allow_empty=True))
        with self._approve("console_type_text", vm, {"characters": len(value)}):
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
        with self._approve("console_press_keys", vm, {"keys": normalized}):
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
        with self._approve("console_key_state", vm, {"key": normalized, "down": down}):
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
        with self._approve("console_mouse_buttons", vm, {
            "left": left, "right": right, "middle": middle,
        }):
            return self._run(
                "console_mouse_buttons", "console_mouse_buttons",
                {"vm": vm, "left": left, "right": right, "middle": middle}, vm_name=vm,
            )

    def console_wait(self, milliseconds: int) -> dict[str, Any]:
        duration = _integer(milliseconds, "milliseconds", 1, 30_000)
        time.sleep(duration / 1000)
        return {"waited_ms": duration}

    def console_batch(self, vm_name: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_console, "HYPERV_MCP_ALLOW_CONSOLE"
        )
        if not isinstance(actions, list) or not 1 <= len(actions) <= 32:
            raise ValidationError("actions must contain between one and 32 console actions.")
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
        # One approval covers the whole batch; the per-action calls inherit it.
        with self._approve("console_batch", vm, {
            "actions": [kind for _, kind, _ in prepared],
            "action_count": len(prepared),
        }):
            results = []
            for index, kind, arguments in prepared:
                if kind == "text":
                    result = self.console_type_text(vm, arguments["text"])
                elif kind == "keys":
                    result = self.console_press_keys(vm, arguments["keys"])
                elif kind == "click":
                    result = self.console_click(
                        vm, arguments.get("button", "left"), arguments.get("x"), arguments.get("y")
                    )
                elif kind == "move":
                    result = self.console_mouse_move(vm, arguments["x"], arguments["y"])
                elif kind == "scroll":
                    result = self.console_scroll(vm, arguments["delta"])
                elif kind == "wait":
                    result = self.console_wait(arguments["milliseconds"])
                else:
                    result = self.console_ctrl_alt_delete(vm)
                results.append({"index": index, "type": kind, "result": result})
        return {"vm": vm, "actions_completed": len(results), "results": results}

    def console_ctrl_alt_delete(self, vm_name: str) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_console, "HYPERV_MCP_ALLOW_CONSOLE"
        )
        with self._approve("console_ctrl_alt_delete", vm):
            return self._run(
                "console_ctrl_alt_delete", "console_ctrl_alt_delete", {"vm": vm}, vm_name=vm
            )

    def console_mouse_move(self, vm_name: str, x: int, y: int) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_console, "HYPERV_MCP_ALLOW_CONSOLE"
        )
        payload = {
            "vm": vm,
            "x": _integer(x, "x", 0, 32_767),
            "y": _integer(y, "y", 0, 32_767),
        }
        with self._approve("console_mouse_move", vm, {"x": payload["x"], "y": payload["y"]}):
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
        summary = {"button": button, "x": payload.get("x"), "y": payload.get("y")}
        with self._approve("console_click", vm, summary):
            return self._run("console_click", "console_click", payload, vm_name=vm)

    def console_scroll(self, vm_name: str, delta: int) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_console, "HYPERV_MCP_ALLOW_CONSOLE"
        )
        amount = _integer(delta, "delta", -100_000, 100_000)
        with self._approve("console_scroll", vm, {"delta": amount}):
            return self._run(
                "console_scroll", "console_scroll",
                {"vm": vm, "delta": amount}, vm_name=vm,
            )

    def authorize_screenshot(self, vm_name: str) -> str:
        return self._write(
            vm_name, self.settings.allow_screenshots, "HYPERV_MCP_ALLOW_SCREENSHOTS"
        )

    def capture_screenshot(
        self, capture: Any, vm_name: str, region: str = "window", max_width: int = 1600
    ) -> Any:
        name = self.authorize_screenshot(vm_name)
        with self._approve("console_screenshot", name, {"region": region}):
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
        with self._approve("debug_serial_prepare", vm, {"pipe_name": pipe_name}):
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
        with self._approve("debug_guest_prepare", vm, {"bcd_debug": "COM1/115200"}):
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
        with self._approve("serial_connect", vm, {"pipe_name": name, "log_path": log}):
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
        # Typed text may be a password, so the dialog sees only its size.
        with self._approve("serial_send", vm, {
            "characters": len(data), "append_newline": append_newline,
        }):
            return self._serial_action("serial_send", vm, lambda: self.serial.send(vm, data))

    def serial_disconnect(self, vm_name: str) -> dict[str, Any]:
        vm = self._write(
            vm_name, self.settings.allow_debugger, "HYPERV_MCP_ALLOW_DEBUGGER"
        )
        with self._approve("serial_disconnect", vm):
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
        number = _integer(controller_number, "controller_number", 0, 1)
        location = _integer(controller_location, "controller_location", 0, 63)
        summary = {
            "iso_path": path, "boot_first": boot_first,
            "controller_number": number, "controller_location": location,
        }
        with self._approve("media_mount", vm, summary):
            return self._run(
                "media_mount", "media_mount",
                {"vm": vm, "path": path, "controller_number": number,
                 "controller_location": location, "boot_first": boot_first},
                vm_name=vm,
            )

    def media_eject(
        self, vm_name: str, controller_location: int, confirm_vm: str, controller_number: int = 0
    ) -> dict[str, Any]:
        vm = self._write(vm_name, self.settings.allow_media, "HYPERV_MCP_ALLOW_MEDIA")
        self._confirm(confirm_vm, vm, "confirm_vm")
        number = _integer(controller_number, "controller_number", 0, 1)
        location = _integer(controller_location, "controller_location", 0, 63)
        with self._approve("media_eject", vm, {
            "controller_number": number, "controller_location": location,
        }):
            return self._run(
                "media_eject", "media_eject",
                {"vm": vm, "controller_number": number, "controller_location": location},
                vm_name=vm,
            )
