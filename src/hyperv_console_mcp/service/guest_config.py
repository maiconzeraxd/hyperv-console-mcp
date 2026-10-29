"""Guest automation settings: autologon, prompts, power and revert."""

from __future__ import annotations

import time
from typing import Any

from ..errors import BackendError, PolicyError, ValidationError
from ._base import ServiceBase


class GuestConfigService(ServiceBase):
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
        from .. import credentials
        from ..transport import guest_config

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
            self._write(name, "power")

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
        from ..transport import guest_config

        name = self._guest_vm(vm_name)
        return self._call("guest_config_status", name, lambda: guest_config.status(self, name))

    def guest_config_revert(self, vm_name: str, confirm_vm: str, *, restart: bool = False) -> dict[str, Any]:
        """Put every recorded setting back the way it was before this tool touched it."""
        from ..transport import guest_config

        name = self._guest_vm(vm_name)
        self._confirm(confirm_vm, name, "confirm_vm")
        if not isinstance(restart, bool):
            raise ValidationError("restart must be a boolean value.")
        if restart:
            self._write(name, "power")
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
        from ..transport import invalidate_health, select_transport

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
        # The VM was stopped and started again, so whatever the health cache
        # says about SSH is about the machine that no longer exists.
        invalidate_health(self.settings, name)
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
