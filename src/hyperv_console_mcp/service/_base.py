"""Shared policy helpers: allowlist, capability flags, audit and approval."""

from __future__ import annotations

import hashlib
import os
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, TypeVar

from ..audit import AuditLogger
from ..backend import HyperVBackend
from ..config import Settings
from ..errors import (
    HyperVError,
    PolicyError,
    ValidationError,
)
from ..kd.manager import KdManager
from ..serial import SerialManager

MAX_GUEST_FILE_BYTES = 8 * 1024 * 1024
COMMAND_PREVIEW_LINES = 20
# console_wait_for_change polls a small thumbnail rather than the full framebuffer.
WAIT_SAMPLE_WIDTH = 160
WAIT_SAMPLE_SIZE = (64, 36)
WAIT_POLL_SECONDS = 0.4
WAIT_CHANGE_THRESHOLD = 0.01
# Public symbol server; the cache directory is the configured symbol root.
KD_SYMBOL_SERVER = "https://msdl.microsoft.com/download/symbols"
# Every MCP tool annotated MUTATION must request approval here, once per call.
GATED_TOOLS = frozenset({
    "vm_power", "checkpoint", "guest_exec",
    "guest_write_file", "guest_put_file", "guest_get_file",
    "guest_ssh_provision", "guest_ssh_deprovision",
    "guest_credential", "guest_autologon_configure", "guest_config_revert",
    "lab_init", "lab_stage", "lab_collect", "lab_run", "lab_cycle",
    "lab_guest_configure",
    "vm_inject_nmi",
    "console_input",
    "media_mount", "media_eject",
    "serial_connect", "serial_send", "serial_disconnect",
    "debug_setup",
    "kd_attach", "kd_detach", "kd_execution", "kd_command", "kd_breakpoint",
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

class ServiceBase:
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
        self.kd = KdManager()

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
        from ..events import approval_scope
        return approval_scope(operation, vm_name, summary)

    def _local_action(
        self, operation: str, vm_name: str, action: Callable[[], T]
    ) -> T:
        from ..events import EVENTS, require_dashboard_approval
        with EVENTS.operation(operation, vm_name):
            if operation in {"serial_connect", "serial_send", "serial_disconnect", "console_screenshot"} or operation.startswith("kd_"):
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
