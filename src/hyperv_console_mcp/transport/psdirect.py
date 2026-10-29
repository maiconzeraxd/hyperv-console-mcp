"""PowerShell Direct transport: the Hyper-V bridge's guest operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import credentials
from ..errors import ValidationError
from .base import ExecResult, FileResult

# Structured guest operations that run the same guest-side PowerShell over any
# transport; the transport only decides how the request reaches the guest.
GUEST_OPERATIONS = frozenset({
    "guest_processes", "guest_services", "guest_events",
    "guest_read_file", "guest_read_chunk", "guest_assemble_file",
    "guest_write_file", "guest_package_inspect", "guest_package_commit",
})


class PowerShellDirectTransport:
    """Guest access over the persistent bridge worker's `New-PSSession -VMName`."""

    name = "psdirect"

    def __init__(self, backend: Any, settings: Any) -> None:
        self.backend = backend
        self.settings = settings

    def _credentials(self, vm: str) -> dict[str, str]:
        username, password = credentials.resolve(self.settings, vm)
        return {"vm": vm, "username": username, "password": password}

    def _invoke(self, operation: str, payload: dict[str, Any], *, timeout: float | None = None):
        return self.backend.invoke(operation, payload, timeout=timeout or self.settings.timeout_seconds)

    def exec(self, vm: str, command: str, *, timeout: float, max_output: int) -> ExecResult:
        payload = self._credentials(vm)
        payload.update(command=command, max_output_chars=max_output)
        execution = self._invoke("guest_exec", payload, timeout=timeout)["execution"]
        return ExecResult(
            success=bool(execution.get("success")),
            exit_code=int(execution.get("exit_code") or 0),
            output=str(execution.get("output") or ""),
            had_errors=bool(execution.get("had_errors")),
            error_records=tuple(str(item) for item in execution.get("error_records") or ()),
            truncated=bool(execution.get("output_truncated")),
            transport=self.name,
        )

    def health(self, vm: str) -> dict[str, Any]:
        result = self._invoke("guest_status", self._credentials(vm))
        guest = result.get("guest") if isinstance(result.get("guest"), dict) else result
        return {**guest, "transport": self.name}

    def file_info(self, vm: str, remote: str) -> dict[str, Any]:
        payload = self._credentials(vm)
        payload["path"] = remote
        return self._invoke("guest_file_info", payload)["file"]

    def call(self, vm: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one structured guest operation over the persistent session."""
        if operation not in GUEST_OPERATIONS:
            raise ValidationError(f"{operation!r} is not a guest operation.")
        request = self._credentials(vm)
        request.update(payload)
        return self._invoke(operation, request)

    def put_file(self, vm: str, local: Path, remote: str, *, overwrite: bool) -> FileResult:
        """Copy over the persistent session; the guest re-hashes what it received."""
        source = Path(local)
        if not source.is_file():
            raise ValidationError(f"{source} is not a file.")
        payload = self._credentials(vm)
        payload.update(local_path=str(source), guest_path=remote, overwrite=overwrite)
        written = self._invoke("guest_put_file", payload)["file"]
        return FileResult(
            path=str(written.get("path") or remote),
            size=int(written["size"]),
            sha256=str(written["sha256"]),
            transport=self.name,
            bytes_transferred=source.stat().st_size,
        )

    def get_file(self, vm: str, remote: str, local: Path) -> FileResult:
        """Download over the persistent session into a host path, then verify."""
        target = Path(local)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self._credentials(vm)
        payload.update(guest_path=remote, local_path=str(target), overwrite=True)
        fetched = self._invoke("guest_get_file", payload)["file"]
        return FileResult(
            path=str(fetched.get("path") or target),
            size=int(fetched["size"]),
            sha256=str(fetched["sha256"]),
            transport=self.name,
            bytes_transferred=int(fetched["size"]),
        )

    def close(self, vm: str | None = None) -> None:
        # The persistent session lives in the bridge worker and is closed with it.
        return None
