"""Persistent local PowerShell worker with one JSON line per request/response."""

from __future__ import annotations

import atexit
import base64
import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Mapping

from .errors import BackendError

_MARKER = "HVMCP1:"


def worker_environment(
    executable: str, environ: Mapping[str, str] | None = None
) -> dict[str, str] | None:
    """The environment for the worker, with PowerShell 7's modules kept out of 5.1.

    Windows PowerShell 5.1 autoloads a module from the first `PSModulePath`
    entry that holds it, and a parent started by `pwsh` leaves PowerShell 7's
    module directories in the inherited value. Loading PS7's copy of
    `Microsoft.PowerShell.Security` into 5.1 applies a type-data file whose
    members 5.1 already has, so the import fails with "The member AuditToString
    is already present" and every later `ConvertTo-SecureString` reports that
    the module "could not be loaded" — which is how a guest credential is built.

    The entries are dropped, not reordered: a user module in
    `Documents\\WindowsPowerShell\\Modules` or a machine one in
    `Program Files\\WindowsPowerShell\\Modules` is left exactly where it was.
    Anything that is not `powershell.exe` — `pwsh` itself, a test double — is
    given the environment untouched, because for those the entries are correct.
    """
    source = os.environ if environ is None else environ
    if Path(executable).name.casefold() != "powershell.exe":
        return None
    module_path = source.get("PSModulePath")
    if not module_path:
        return None
    kept = [
        entry
        for entry in module_path.split(os.pathsep)
        if not _is_powershell_seven_modules(entry)
    ]
    if len(kept) == len(module_path.split(os.pathsep)):
        return None
    environment = dict(source)
    environment["PSModulePath"] = os.pathsep.join(kept)
    return environment


def _is_powershell_seven_modules(entry: str) -> bool:
    """True for a PowerShell 7 module directory, false for 5.1's own."""
    normalized = entry.replace("/", "\\").casefold().rstrip("\\")
    if "\\windowspowershell\\" in normalized:
        return False
    return "\\powershell\\" in normalized or normalized.endswith("\\powershell")


class HyperVBackend:
    """Structural interface used by services and their fake test backends."""

    def invoke(
        self, operation: str, payload: Mapping[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        raise NotImplementedError


# Every guest-side bridge operation shares this prefix; the host-side SSH
# operations are named ssh_* and stay on the host worker.
GUEST_PREFIX = "guest_"


class GuestSplitBackend:
    """Two workers, so a long guest command cannot block host and console calls.

    Guest operations keep their own persistent worker and therefore their own
    PowerShell Direct session per VM; everything else stays on the host worker.
    Both workers start lazily, so a session that never touches the guest runs
    exactly one process.
    """

    def __init__(
        self,
        host: HyperVBackend | None = None,
        guest: HyperVBackend | None = None,
        *,
        script_path: Path | None = None,
        executable: str = "powershell.exe",
    ) -> None:
        self.host = host if host is not None else PowerShellBackend(script_path, executable)
        self.guest = guest if guest is not None else PowerShellBackend(script_path, executable)

    def worker(self, operation: str) -> HyperVBackend:
        return self.guest if operation.startswith(GUEST_PREFIX) else self.host

    def invoke(
        self, operation: str, payload: Mapping[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        return self.worker(operation).invoke(operation, payload, timeout=timeout)

    def close(self) -> None:
        for worker in (self.host, self.guest):
            close = getattr(worker, "close", None)
            if callable(close):
                close()


# A primary backend that answers `wmi_unsupported` or `wmi_unavailable` has
# simply not taken that operation over yet; anything else is a real failure.
FALLBACK_CODES = frozenset({"wmi_unsupported", "wmi_unavailable"})


class FallbackBackend:
    """The migrated path first, the PowerShell worker for what it refuses.

    Migrating a host operation means writing it in Python and deleting it from
    the bridge. Until then both have to answer, so this composes them instead of
    replacing one with the other, and an operation moves the moment the primary
    stops refusing it.
    """

    def __init__(
        self,
        primary: HyperVBackend,
        fallback: HyperVBackend,
        *,
        codes: frozenset[str] = FALLBACK_CODES,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.codes = codes

    def invoke(
        self, operation: str, payload: Mapping[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        try:
            return self.primary.invoke(operation, payload, timeout=timeout)
        except BackendError as exc:
            if exc.code not in self.codes:
                raise
        return self.fallback.invoke(operation, payload, timeout=timeout)

    def close(self) -> None:
        for worker in (self.primary, self.fallback):
            close = getattr(worker, "close", None)
            if callable(close):
                close()


class PowerShellBackend:
    """Reuses one finite, non-interactive PowerShell worker for low latency."""
    def __init__(
        self,
        script_path: Path | None = None,
        executable: str = "powershell.exe",
    ) -> None:
        self.script_path = script_path or Path(__file__).with_name("bridge") / "HyperVBridge.ps1"
        self.executable = executable
        if not self.script_path.is_file():
            raise BackendError(f"PowerShell bridge is missing: {self.script_path}")
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[str | BaseException | None] = queue.Queue()
        self._errors: deque[str] = deque(maxlen=40)
        atexit.register(self.close)

    def _start(self) -> subprocess.Popen[str]:
        command = [
            self.executable, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(self.script_path), "-Server",
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                cwd=str(self.script_path.parent),
                env=worker_environment(self.executable),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise BackendError(
                f"Could not start Windows PowerShell: {exc}", code="bridge_transport"
            ) from exc
        self._process = process
        responses: queue.Queue[str | BaseException | None] = queue.Queue()
        self._responses = responses
        threading.Thread(
            target=self._read_stdout, args=(process, responses), daemon=True, name="hvmcp-stdout"
        ).start()
        threading.Thread(
            target=self._read_stderr, args=(process,), daemon=True, name="hvmcp-stderr"
        ).start()
        return process

    def _read_stdout(
        self,
        process: subprocess.Popen[str],
        responses: queue.Queue[str | BaseException | None],
    ) -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                responses.put(line)
        except BaseException as exc:
            responses.put(exc)
        finally:
            responses.put(None)

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        try:
            assert process.stderr is not None
            for line in process.stderr:
                self._errors.append(line.strip())
        except (OSError, ValueError):
            pass

    def invoke(
        self, operation: str, payload: Mapping[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        if not operation or not operation.replace("_", "").isalnum():
            raise BackendError("Invalid internal bridge operation.", code="bridge_transport")
        request = json.dumps(
            {"version": 1, "operation": operation, **payload}, ensure_ascii=True, separators=(",", ":")
        )
        with self._lock:
            try:
                return self._invoke_locked(operation, request, timeout or 90)
            except BackendError as exc:
                if exc.code in {"bridge_transport", "timeout"}:
                    self._stop_locked()
                raise
            except BaseException:
                self._stop_locked()
                raise

    def _invoke_locked(self, operation: str, request: str, timeout: float) -> dict[str, Any]:
        process = self._process if self._process and self._process.poll() is None else self._start()
        if not process.stdin:
            raise BackendError("The bridge stdin is unavailable.", code="bridge_transport")
        try:
            process.stdin.write(request + "\n")
            process.stdin.flush()
            deadline = time.monotonic() + timeout
            while True:
                line = self._responses.get(timeout=max(0, deadline - time.monotonic()))
                if isinstance(line, str) and not line.startswith(_MARKER):
                    self._errors.append("Non-protocol PowerShell output: " + line.strip()[:2000])
                    continue
                break
        except queue.Empty as exc:
            raise BackendError(
                "The local bridge timed out. A guest command may still be running; "
                "inspect the guest before retrying.",
                code="timeout",
            ) from exc
        except (OSError, ValueError) as exc:
            raise BackendError(
                f"Could not communicate with the PowerShell worker: {exc}",
                code="bridge_transport",
            ) from exc
        if isinstance(line, BaseException):
            raise BackendError("The bridge reader failed.", code="bridge_transport") from line
        if line is None:
            detail = "\n".join(self._errors)[-2000:]
            raise BackendError(
                f"The PowerShell worker exited unexpectedly. {detail}", code="bridge_transport"
            )
        response = self._decode(line)
        if response is None:
            raise BackendError(
                f"The PowerShell worker returned an invalid transport response: {line[-2000:]}",
                code="bridge_transport",
            )
        if not response.get("ok"):
            error = response.get("error") or {}
            raise BackendError(
                str(error.get("message") or "The Hyper-V operation failed."),
                code=str(error.get("code") or "backend_error"),
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise BackendError("The bridge returned an invalid result.", code="bridge_transport")
        return result

    def close(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()

    @staticmethod
    def _decode(output: str) -> dict[str, Any] | None:
        for line in reversed(output.splitlines()):
            if not line.startswith(_MARKER):
                continue
            try:
                raw = base64.b64decode(line[len(_MARKER) :], validate=True)
                value = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise BackendError(
                    "The PowerShell bridge returned malformed JSON.", code="bridge_transport"
                ) from exc
            if isinstance(value, dict):
                return value
        return None
