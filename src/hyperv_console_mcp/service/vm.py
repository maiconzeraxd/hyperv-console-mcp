"""VM inventory, power, checkpoints and operation status."""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from ..errors import HyperVError, ValidationError
from ._base import ServiceBase, _integer, _text


class VmService(ServiceBase):
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
        from ..events import EVENTS
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
        # The flags describe a VM with no access level. A VM that has one takes
        # its capabilities from that level instead, which is why the map below
        # is reported alongside rather than folded into this answer.
        result["capabilities"] = {
            "power": self.settings.allow_power,
            "checkpoints": self.settings.allow_checkpoints,
            "guest": self.settings.allow_guest,
            "console": self.settings.allow_console,
            "screenshots": self.settings.allow_screenshots,
            "debugger": self.settings.allow_debugger,
            "media": self.settings.allow_media,
        }
        result["levels"] = dict(sorted(self.settings.levels.items()))
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
        name = self._write(vm_name, "power")
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
        vm = self._write(vm_name, "checkpoints")
        checkpoint_name = _text(name, "name", 128)
        with self._approve("checkpoint", vm, {"action": "create", "checkpoint_name": checkpoint_name}):
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
        vm = self._write(vm_name, "checkpoints")
        snapshot = self._snapshot_id(snapshot_id)
        self._confirm(confirm_vm, vm, "confirm_vm")
        self._confirm(confirm_snapshot_id, snapshot, "confirm_snapshot_id")
        with self._approve("checkpoint", vm, {"action": "restore", "snapshot_id": snapshot}):
            return self._run(
                "checkpoint_restore", "checkpoint_restore",
                {"vm": vm, "snapshot_id": snapshot}, vm_name=vm,
            )

    def delete_checkpoint(
        self, vm_name: str, snapshot_id: str, confirm_vm: str, confirm_snapshot_id: str
    ) -> dict[str, Any]:
        vm = self._write(vm_name, "checkpoints")
        snapshot = self._snapshot_id(snapshot_id)
        self._confirm(confirm_vm, vm, "confirm_vm")
        self._confirm(confirm_snapshot_id, snapshot, "confirm_snapshot_id")
        with self._approve("checkpoint", vm, {"action": "delete", "snapshot_id": snapshot}):
            return self._run(
                "checkpoint_delete", "checkpoint_delete",
                {"vm": vm, "snapshot_id": snapshot}, vm_name=vm,
            )
