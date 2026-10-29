"""The optional SSH guest transport."""

from __future__ import annotations

from typing import Any

from ..errors import ValidationError
from ._base import ServiceBase


class SshService(ServiceBase):
    def guest_ssh_provision(
        self, vm_name: str, confirm_vm: str, *, use_existing_adapter: bool = False
    ) -> dict[str, Any]:
        """Create key-only SSH in the guest and pin its host key on the host."""
        from ..transport import ssh_setup

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
        from ..transport import ssh_setup

        name = self._guest_vm(vm_name)
        return self._call("guest_ssh_status", name, lambda: ssh_setup.status(self, name))

    def guest_ssh_deprovision(self, vm_name: str, confirm_vm: str) -> dict[str, Any]:
        from ..transport import ssh_setup

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
