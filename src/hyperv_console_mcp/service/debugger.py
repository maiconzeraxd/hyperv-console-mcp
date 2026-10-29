"""The kd.exe session: attach, execution, breakpoints and analysis."""

from __future__ import annotations

import hashlib
import re
from importlib import import_module
from pathlib import Path
from typing import Any

from ..errors import DebuggerError, PolicyError, ValidationError
from ..kd import analyze_report, kdnet
from ..kd.paths import session_log_path
from ._base import KD_SYMBOL_SERVER, ServiceBase, _confined_path, _integer, _text


def _tool(name: str) -> Any:
    """Resolve a debugger tool the package re-exports, at call time.

    ``hyperv_console_mcp.service`` names ``find_debugger``, ``KdSession`` and
    ``run_dump`` once; resolving them here keeps that one naming point, so a
    caller that substitutes a host without kd.exe is honoured on the next call
    instead of at import time.
    """
    return getattr(import_module(__package__), name)


class DebuggerService(ServiceBase):
    def _kd_gate(self, vm_name: str) -> str:
        return self._write(vm_name, "debugger")

    def _kd_timeout(self, timeout_ms: int | None) -> float:
        if timeout_ms is None:
            return float(self.settings.kd_timeout_seconds)
        return _integer(timeout_ms, "timeout_ms", 250, 3_600_000) / 1_000.0

    def _symbol_path(self) -> str:
        """Public symbol server with a local cache, plus any staged lab package.

        A staged package keeps the driver's private PDB next to it on the host,
        so its directory is appended and the stack shows real names.
        """
        cache = Path(self.settings.symbol_root)
        try:
            cache.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        entries = [f"srv*{cache}*{KD_SYMBOL_SERVER}"]
        lab = Path(self.settings.lab_root)
        try:
            children = sorted(lab.iterdir()) if lab.is_dir() else []
        except OSError:
            children = []
        for child in children:
            try:
                if child.is_dir() and next(child.glob("*.pdb"), None) is not None:
                    entries.append(str(child))
            except OSError:
                continue
        return ";".join(entries)

    def kd_attach(
        self,
        vm_name: str,
        transport: str = "kdnet",
        break_on_connect: bool = False,
        initial_break: bool = False,
    ) -> dict[str, Any]:
        vm = self._kd_gate(vm_name)
        if transport not in {"serial", "kdnet"}:
            raise ValidationError("transport must be serial or kdnet.")
        for label, value in (
            ("break_on_connect", break_on_connect), ("initial_break", initial_break),
        ):
            if not isinstance(value, bool):
                raise ValidationError(f"{label} must be a boolean value.")
        kd_path = _tool("find_debugger")("kd", directory=self.settings.debuggers_dir)
        command = [str(kd_path)]
        details: dict[str, Any] = {}
        serial_detached = False
        if transport == "kdnet":
            state = kdnet.read_state(self.settings, vm)
            if state is None:
                raise PolicyError(
                    f"No KDNET state for {vm}. Run debug_kdnet_prepare first, then reboot the guest."
                )
            key = kdnet.key_store().read(vm)
            if not key:
                raise PolicyError(
                    f"The KDNET key for {vm} is not in the Credential Manager. "
                    "Run debug_kdnet_prepare again."
                )
            port = int(state["port"])
            command += ["-k", f"net:port={port},key={key}"]
            details = {"port": port, "host_ip": state.get("host_ip")}
        else:
            status = self.debug_serial_status(vm)
            ports = [item for item in status.get("com_ports", []) if item.get("number") == 1]
            if len(ports) != 1 or not ports[0].get("path"):
                raise ValidationError(
                    "COM1 is not configured with a debugger pipe. Run debug_serial_prepare first."
                )
            if str(status.get("state", "")).casefold() != "running":
                raise ValidationError("The VM must be Running before attaching kd over the serial pipe.")
            pipe = str(ports[0]["path"])
            command += ["-k", f"com:pipe,port={pipe},baud=115200,reconnect,resets=0"]
            details = {"pipe_path": pipe}
        if break_on_connect:
            command.append("-b")
        log = session_log_path(self.settings, vm)
        symbol_path = self._symbol_path()

        def action() -> dict[str, Any]:
            nonlocal serial_detached
            if transport == "serial" and self.serial.status(vm).get("connected"):
                # One reader per pipe: the kd console owns it now.
                self.serial.disconnect(vm)
                serial_detached = True
            session = self.kd.start(
                vm,
                lambda: _tool("KdSession")(
                    command,
                    log_path=log,
                    vm_name=vm,
                    transport=transport,
                    symbol_path=symbol_path,
                    max_output_chars=self.settings.max_output_chars,
                    timeout=self.settings.kd_timeout_seconds,
                ),
                startup_timeout=self.settings.kd_startup_timeout_seconds,
            )
            result: dict[str, Any] = {"attached": True, **session.status()}
            if initial_break:
                result["break_in"] = session.break_in()
            return result

        with self._approve("kd_attach", vm, {
            "transport": transport, "log_path": str(log), **details,
        }):
            result = self._local_action("kd_attach", vm, action)
        return {**result, "serial_detached": serial_detached}

    def kd_status(self, vm_name: str) -> dict[str, Any]:
        name = self._vm(vm_name)
        session = self.kd.get(name)
        result: dict[str, Any] = {
            "vm": name,
            "attached": session is not None,
            "serial": self.serial.status(name),
        }
        if session is not None:
            result.update(session.status())
        return result

    def kd_detach(self, vm_name: str) -> dict[str, Any]:
        vm = self._kd_gate(vm_name)
        session = self.kd.get(vm)
        with self._approve("kd_detach", vm, {"attached": session is not None}):
            result = self._local_action(
                "kd_detach", vm, lambda: self.kd.stop(vm, detach=True)
            )
        return {"vm": vm, **result}

    def kd_break(
        self, vm_name: str, nmi: bool | None = None, timeout_ms: int | None = None
    ) -> dict[str, Any]:
        vm = self._kd_gate(vm_name)
        if nmi is not None and not isinstance(nmi, bool):
            raise ValidationError("nmi must be a boolean value or omitted.")
        session = self.kd.require(vm)
        timeout = self._kd_timeout(timeout_ms)

        def action() -> dict[str, Any]:
            if session.state == "broken":
                return {"method": "already_broken", "broke": True, "state": session.state}
            try:
                return {"method": "break", **session.break_in(timeout=min(timeout, 15.0))}
            except DebuggerError:
                # The console break-in is the gentle path; an NMI is the one kd
                # always receives, at the cost of interrupting the guest.
                if nmi is False or session.state != "running":
                    raise
                self._run("kd_break", "vm_inject_nmi", {"vm": vm}, vm_name=vm, audit=False)
                return {"method": "nmi", **session.break_in(timeout=timeout)}

        with self._approve("kd_execution", vm, {"state": session.state, "nmi": nmi}):
            result = self._local_action("kd_break", vm, action)
        return {"vm": vm, **result}

    def kd_go(
        self, vm_name: str, command: str = "g", timeout_ms: int | None = None
    ) -> dict[str, Any]:
        vm = self._kd_gate(vm_name)
        session = self.kd.require(vm)
        text = _text(command, "command", 32)
        timeout = self._kd_timeout(timeout_ms)
        with self._approve("kd_execution", vm, {"command": text, "timeout_seconds": timeout}):
            result = self._local_action(
                "kd_go", vm, lambda: session.go(text, timeout=timeout)
            )
        return {"vm": vm, **result}

    def kd_command(
        self, vm_name: str, command: str, timeout_ms: int | None = None
    ) -> dict[str, Any]:
        vm = self._kd_gate(vm_name)
        session = self.kd.require(vm)
        text = _text(command, "command", 4_096)
        if "\n" in text or "\r" in text:
            raise ValidationError("command must be a single line.")
        timeout = self._kd_timeout(timeout_ms)
        summary = {
            "command": text[:400],
            "command_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "timeout_seconds": timeout,
        }
        with self._approve("kd_command", vm, summary):
            result = self._local_action(
                "kd_command", vm, lambda: session.command(text, timeout=timeout)
            )
        return {"vm": vm, **result}

    def kd_wait_event(
        self, vm_name: str, timeout_ms: int | None = None
    ) -> dict[str, Any]:
        vm = self._kd_gate(vm_name)
        session = self.kd.require(vm)
        timeout = self._kd_timeout(timeout_ms)
        result = self._local_action(
            "kd_wait_event", vm, lambda: session.wait_event(timeout=timeout)
        )
        return {"vm": vm, **result}

    def kd_set_breakpoint(
        self,
        vm_name: str,
        target: str,
        deferred: bool = False,
        command: str | None = None,
    ) -> dict[str, Any]:
        vm = self._kd_gate(vm_name)
        session = self.kd.require(vm)
        text = _text(target, "target", 512)
        if not re.fullmatch(r"[^\s;\"]+", text):
            raise ValidationError("target must be a single symbol, module!symbol, or address.")
        if not isinstance(deferred, bool):
            raise ValidationError("deferred must be a boolean value.")
        line = f"{'bu' if deferred else 'bp'} {text}"
        if command is not None:
            hook = _text(command, "command", 512)
            if '"' in hook or "\n" in hook or "\r" in hook:
                raise ValidationError("command must be one line without double quotes.")
            line += f' "{hook}"'
        with self._approve("kd_breakpoint", vm, {"breakpoint": line}):
            result = self._local_action(
                "kd_set_breakpoint", vm, lambda: session.command(line)
            )
        return {"vm": vm, "breakpoint": line, **result}

    def kd_clear_breakpoints(self, vm_name: str) -> dict[str, Any]:
        vm = self._kd_gate(vm_name)
        session = self.kd.require(vm)
        with self._approve("kd_breakpoint", vm, {"breakpoint": "all"}):
            result = self._local_action(
                "kd_clear_breakpoints", vm, lambda: session.command("bc *")
            )
        return {"vm": vm, **result}

    def kd_break_on_load(
        self, vm_name: str, module: str, enabled: bool = True
    ) -> dict[str, Any]:
        vm = self._kd_gate(vm_name)
        session = self.kd.require(vm)
        name = _text(module, "module", 128)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ValidationError("module must be a module name without a path or extension tricks.")
        if not isinstance(enabled, bool):
            raise ValidationError("enabled must be a boolean value.")
        line = f"{'sxe' if enabled else 'sxd'} ld:{name}"
        with self._approve("kd_breakpoint", vm, {"command": line}):
            result = self._local_action(
                "kd_break_on_load", vm, lambda: session.command(line)
            )
        return {"vm": vm, "command": line, **result}

    def kd_stack(self, vm_name: str, depth: int = 20) -> dict[str, Any]:
        vm = self._kd_gate(vm_name)
        session = self.kd.require(vm)
        frames = _integer(depth, "depth", 1, 100)
        result = self._local_action(
            "kd_stack", vm, lambda: session.command(f"k {frames}")
        )
        return {"vm": vm, **result}

    def kd_modules(self, vm_name: str) -> dict[str, Any]:
        vm = self._kd_gate(vm_name)
        session = self.kd.require(vm)
        result = self._local_action("kd_modules", vm, lambda: session.command("lm"))
        return {"vm": vm, **result}

    def kd_analyze(self, vm_name: str) -> dict[str, Any]:
        vm = self._kd_gate(vm_name)
        session = self.kd.require(vm)

        def action() -> dict[str, Any]:
            result = session.command("!analyze -v")
            return {**result, "report": analyze_report(str(result.get("output", "")))}

        return {"vm": vm, **self._local_action("kd_analyze", vm, action)}

    def kd_dump_open(
        self,
        vm_name: str,
        dump_path: str,
        commands: list[str] | None = None,
    ) -> dict[str, Any]:
        vm = self._vm(vm_name)
        dump = _confined_path(dump_path, "dump_path", self.settings.evidence_root, ".dmp")
        kd_path = _tool("find_debugger")("kd", directory=self.settings.debuggers_dir)
        if commands is None:
            steps: list[str] = ["!analyze -v"]
        else:
            if not isinstance(commands, (list, tuple)) or not commands:
                raise ValidationError("commands must be a non-empty list of kd commands.")
            steps = []
            for item in commands:
                text = _text(item, "command", 512)
                if "\n" in text or "\r" in text:
                    raise ValidationError("each command must be a single line.")
                steps.append(text)

        def action() -> dict[str, Any]:
            result = _tool("run_dump")(
                kd_path, dump,
                commands=steps,
                timeout=float(self.settings.kd_timeout_seconds) * 2,
                symbol_path=self._symbol_path(),
                max_output_chars=max(self.settings.max_output_chars, 262_144),
            )
            return {**result, "report": analyze_report(str(result.get("output", "")))}

        return {"vm": vm, **self._local_action("kd_dump_open", vm, action)}
