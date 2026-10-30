"""Shared policy helpers: allowlist, capability flags, audit and approval."""

from __future__ import annotations

import hashlib
import os
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, TypeVar

from ..access import minimum_level
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
    "vm_power", "checkpoint", "guest_exec", "guest_transport",
    "guest_write_file", "guest_put_file", "guest_get_file",
    "guest_make_dir", "guest_rename", "guest_delete",
    "host_make_dir", "host_rename", "host_delete",
    "guest_ssh_provision", "guest_ssh_deprovision",
    "guest_credential", "guest_autologon_configure", "guest_config_revert",
    "lab_init", "lab_stage", "lab_collect", "lab_run", "lab_cycle", "lab_matrix",
    "lab_guest_configure", "lab_verifier", "lab_driver_service",
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


# The host directories this server may touch by path: a package to stage,
# evidence to collect, a transfer to land, media to mount. The file manager
# browses exactly these, and a transfer may read from or write into only these,
# so a path that names C:\Windows fails here rather than in the page.
HOST_ROOTS: dict[str, str] = {
    "lab": "lab_root",
    "evidence": "evidence_root",
    "transfer": "transfer_root",
    "media": "iso_root",
}


def _confined_to_roots(value: str, label: str, settings: Any) -> str:
    """Resolve a host path, requiring it to sit inside one of the host roots."""
    text = _text(value, label, 32_768)
    full = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(text)))).resolve()
    roots = [
        Path(
            os.path.abspath(
                os.path.expandvars(os.path.expanduser(str(getattr(settings, field))))
            )
        ).resolve()
        for field in HOST_ROOTS.values()
    ]
    for root in roots:
        if full == root or full.is_relative_to(root):
            return str(full)
    raise PolicyError(
        f"{label} must remain under a configured root: "
        + ", ".join(str(root) for root in roots)
    )


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
        capture: Any = None,
    ) -> None:
        self.backend = backend
        self.settings = settings or Settings()
        self.audit = audit or AuditLogger(self.settings.audit_log)
        self.serial = serial if serial is not None else SerialManager()
        self.kd = KdManager()
        if capture is None:
            from ..capture import VMConnectCapture
            capture = VMConnectCapture()
        self.capture = capture

    def event_snapshot(self, after: int = 0) -> dict[str, Any]:
        """The operation events this process has seen, which is the host's.

        The server used to read its own bus; now that the service can live in
        another process, the caller asks the owner instead of a copy that would
        always be empty.
        """
        from ..events import EVENTS
        return EVENTS.snapshot(after)

    def events_wait(self, after: int = 0, timeout_seconds: float = 15.0) -> dict[str, Any]:
        """The same events, but held open until something arrives or time runs out.

        This is what a live feed needs and a snapshot cannot give: the caller
        asks once and is answered when there is news, instead of asking again
        every few hundred milliseconds and being told "nothing" each time. The
        bus already implements the wait, so this is a door onto it rather than a
        second bus.
        """
        from ..events import EVENTS
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise ValidationError("after must be a non-negative integer.")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValidationError("timeout_seconds must be a number.")
        # A window that asks for an hour would hold a worker thread for an hour.
        return EVENTS.wait(after, min(max(float(timeout_seconds), 0.0), 30.0))

    def logs_subscribe(
        self,
        after: int = 0,
        *,
        vm: str | None = None,
        source: str | None = None,
        level: str | None = None,
        correlation_id: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        """The unified log stream, from an offset, optionally held open.

        The event bus says an operation started and finished; this says what the
        subsystems did inside it. One door, one offset, the same filters a dock
        offers, and the same shape as ``events_wait`` so a client can subscribe
        to both the same way.
        """
        from ..logs import LEVELS, LOGS, SOURCES

        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise ValidationError("after must be a non-negative integer.")
        if source is not None and str(source).casefold() not in SOURCES:
            raise ValidationError(
                f"Unknown log source {source!r}. Known: {', '.join(sorted(SOURCES))}."
            )
        if level is not None and str(level).casefold() not in LEVELS:
            raise ValidationError(
                f"Unknown log level {level!r}. Known: {', '.join(LEVELS)}."
            )
        if correlation_id is not None and not isinstance(correlation_id, str):
            raise ValidationError("correlation_id must be a string.")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValidationError("timeout_seconds must be a number.")
        # A window that asks for an hour would hold a worker thread for an hour.
        return LOGS.wait(
            after,
            timeout_seconds=min(max(float(timeout_seconds), 0.0), 30.0),
            vm=vm,
            source=source,
            level=level,
            correlation_id=correlation_id,
        )

    def reload_policy(self) -> dict[str, Any]:
        """Re-read the environment and the operator's access file.

        A level is chosen while the service is running — from the window, or from
        ``hyperv-access`` in a terminal — so a policy read once at startup would
        need a restart to mean anything. Both writers call this after they save.
        It can only re-read what the operator wrote: nothing here can widen the
        policy by itself, which is why an AI call may ask for it too.
        """
        self.settings = Settings.from_env()
        return {
            "levels": dict(self.settings.levels),
            "allowlist": sorted(self.settings.allowed_vms),
        }

    def _vm(self, name: str) -> str:
        name = _text(name, "vm_name", 256)
        if self.settings.allowed_vms and not self.settings.is_allowed(name):
            raise PolicyError(
                f"VM {name!r} is not allowed. Name it in HYPERV_MCP_ALLOWED_VMS or give it "
                f"an access level with: hyperv-access set \"{name}\" read-only|control|lab",
                fix={"operator": f'hyperv-access set "{name}" read-only|control|lab'},
            )
        return name

    def _write(self, name: str, capability: str) -> str:
        """A mutating operation on a VM, which needs the allowlist and a level."""
        name = self._vm(name)
        if not self.settings.allowed_vms:
            raise PolicyError(
                "Mutations require a VM allowlist: name the VMs in HYPERV_MCP_ALLOWED_VMS, "
                "or give them access levels with hyperv-access set.",
                fix={"operator": f'hyperv-access set "{name}" read-only|control|lab'},
            )
        if not self.settings.allows(name, capability):
            raise PolicyError(
                self.settings.refusal(name, capability),
                fix={"operator": self._raise_to(name, capability)},
            )
        return name

    def _raise_to(self, name: str, capability: str) -> str:
        """The one command that would let this VM do this, and nothing wider.

        A level is the narrow answer; the per-capability variable is the older
        one, offered only where no level exists, because it grants the
        capability to every VM at once.
        """
        level = minimum_level(capability)
        if self.settings.level_for(name) is None:
            return (
                f'hyperv-access set "{name}" {level} '
                f"(or set HYPERV_MCP_ALLOW_{capability.upper()}=true)"
            )
        return f'hyperv-access set "{name}" {level}'

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
        self, operation: str, vm_name: str | None, summary: dict[str, Any] | None = None
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
