"""Kernel-debug preparation: the COM1 pipe, KDNET, and the NMI fallback."""

from __future__ import annotations

import re
from typing import Any

from ..errors import PolicyError, ValidationError
from ..kd import kdnet
from ._base import ServiceBase, _integer


class DebugSetupService(ServiceBase):
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
        with self._approve("debug_setup", vm, {
                "transport": "serial", "action": "prepare", "pipe_name": pipe_name,
            }):
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
        with self._approve("debug_setup", vm, {
                "transport": "serial", "action": "guest", "bcd_debug": "COM1/115200",
            }):
            return self._run(
                "debug_guest_prepare", "debug_guest_prepare",
                {
                    "vm": vm,
                    "username": self.settings.guest_username,
                    "password": self.settings.guest_password,
                },
                vm_name=vm,
            )

    def vm_inject_nmi(self, vm_name: str, confirm_vm: str) -> dict[str, Any]:
        """Force a bugcheck in a hung guest so the debugger can catch it."""
        name = self._write(vm_name, self.settings.allow_debugger, "HYPERV_MCP_ALLOW_DEBUGGER")
        self._confirm(confirm_vm, name, "confirm_vm")
        with self._approve("vm_inject_nmi", name, {"action": "inject_nmi"}):
            return self._run("vm_inject_nmi", "vm_inject_nmi", {"vm": name}, vm_name=name)

    def debug_kdnet_status(self, vm_name: str) -> dict[str, Any]:
        name = self._vm(vm_name)
        result = kdnet.status(self, name)
        result["attached"] = self.kd.get(name) is not None
        return result

    def debug_kdnet_prepare(
        self,
        vm_name: str,
        confirm_vm: str,
        port: int | None = None,
        use_existing_adapter: bool = False,
    ) -> dict[str, Any]:
        vm = self._kd_gate(vm_name)
        self._confirm(confirm_vm, vm, "confirm_vm")
        if port is not None:
            port = _integer(port, "port", kdnet.PORT_MIN, kdnet.PORT_MAX)
        if not isinstance(use_existing_adapter, bool):
            raise ValidationError("use_existing_adapter must be a boolean value.")
        if self.kd.get(vm) is not None:
            raise ValidationError(
                "A debugger session is attached. Detach it before changing the guest's debug settings."
            )
        with self._approve("debug_setup", vm, {
            "transport": "kdnet", "action": "prepare",
            "port": port, "use_existing_adapter": use_existing_adapter,
            "guest_bcd": "net", "key_storage": "credential_manager",
        }):
            return self._call(
                "debug_kdnet_prepare", vm,
                lambda: kdnet.prepare(
                    self, vm, use_existing_adapter=use_existing_adapter, port=port
                ),
            )

    def debug_kdnet_deprovision(self, vm_name: str, confirm_vm: str) -> dict[str, Any]:
        vm = self._kd_gate(vm_name)
        self._confirm(confirm_vm, vm, "confirm_vm")
        detached = False
        with self._approve("debug_setup", vm, {"transport": "kdnet", "action": "deprovision", "removes": [
            "firewall_rule", "guest_bcd_debug", "kdnet_key",
        ]}):
            def action() -> dict[str, Any]:
                nonlocal detached
                if self.kd.get(vm) is not None:
                    self.kd.stop(vm, detach=True)
                    detached = True
                return kdnet.deprovision(self, vm)
            result = self._call("debug_kdnet_deprovision", vm, action)
        return {**result, "detached": detached}
