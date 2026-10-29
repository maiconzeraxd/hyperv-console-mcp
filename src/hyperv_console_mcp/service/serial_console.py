"""The COM1 serial console: attach, read, send, detach."""

from __future__ import annotations

import re
from typing import Any

from ..errors import ValidationError
from ._base import ServiceBase, _confined_path, _integer, _text


class SerialConsoleService(ServiceBase):
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
        if self.kd.get(vm) is not None:
            raise ValidationError(
                "A kd.exe session owns this VM's COM1 pipe. Detach it before connecting the serial console."
            )
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
