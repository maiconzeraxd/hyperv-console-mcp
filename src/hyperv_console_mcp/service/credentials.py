"""The stored guest credential: status, set, forget and rotation."""

from __future__ import annotations

from typing import Any

from ..errors import BackendError, HyperVError, PolicyError, ValidationError
from ._base import ServiceBase, _text


class CredentialService(ServiceBase):
    def guest_credential_status(self, vm_name: str) -> dict[str, Any]:
        """Where the guest credential comes from, and whether it still works."""
        from .. import credentials

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
            from ..transport.ssh import SshTransport
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
        from .. import credentials

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
        from .. import credentials

        name = self._guest_vm(vm_name)
        self._confirm(confirm_vm, name, "confirm_vm")
        with self._approve("guest_credential", name, {"vm": name, "action": "forget", "removes": "vault_entry"}):
            removed = credentials.CredentialStore().delete(name)
            self.audit.record("guest_credential_forget", name, "success")
        return {
            "vm": name, "removed": removed,
            "fallback": credentials.source(self.settings, name),
        }

    def guest_password_rotate(self, vm_name: str, confirm_vm: str) -> dict[str, Any]:
        """Set a new random guest password, prove it works, then store it."""
        from .. import credentials

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
        with self._approve("guest_credential", name, {"action": "rotate", **summary}):
            return self._call(
                "guest_password_rotate", name,
                lambda: self._rotate(name, username, previous_password, previous_source),
            )

    def _rotate(
        self, name: str, username: str, previous_password: str, previous_source: str
    ) -> dict[str, Any]:
        from .. import credentials

        # Fail before changing anything if the guest is unreachable.
        self._on_guest(name, lambda transport: transport.health(name))
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
