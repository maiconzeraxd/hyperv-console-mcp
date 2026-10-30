"""PowerShell Direct transport: the Hyper-V bridge's guest operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import credentials
from ..errors import ValidationError
from .base import ExecResult, FileResult, ProgressCallback

# Structured guest operations that run the same guest-side PowerShell over any
# transport; the transport only decides how the request reaches the guest.
GUEST_OPERATIONS = frozenset({
    "guest_processes", "guest_services", "guest_events",
    "guest_read_file", "guest_read_chunk", "guest_assemble_file",
    "guest_write_file", "guest_package_inspect", "guest_package_list",
    "guest_package_commit",
    # The file manager's half of the contract; the guest side is the same
    # script either transport runs.
    "guest_list_dir", "guest_make_dir", "guest_rename", "guest_delete",
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

    def put_file(
        self, vm: str, local: Path, remote: str, *, overwrite: bool,
        on_progress: ProgressCallback | None = None,
    ) -> FileResult:
        """Copy over the persistent session; the guest re-hashes what it received."""
        source = Path(local)
        if not source.is_file():
            raise ValidationError(f"{source} is not a file.")
        size = source.stat().st_size
        if on_progress is not None:
            # Copy-Item reports nothing until it returns, so the honest report
            # is the two ends of the copy rather than a number that pretends.
            on_progress(0, size)
        payload = self._credentials(vm)
        payload.update(local_path=str(source), guest_path=remote, overwrite=overwrite)
        written = self._invoke("guest_put_file", payload)["file"]
        if on_progress is not None:
            on_progress(size, size)
        return FileResult(
            path=str(written.get("path") or remote),
            size=int(written["size"]),
            sha256=str(written["sha256"]),
            transport=self.name,
            bytes_transferred=source.stat().st_size,
        )

    def get_file(
        self, vm: str, remote: str, local: Path,
        on_progress: ProgressCallback | None = None,
    ) -> FileResult:
        """Download over the persistent session into a host path, then verify."""
        target = Path(local)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self._credentials(vm)
        payload.update(guest_path=remote, local_path=str(target), overwrite=True)
        fetched = self._invoke("guest_get_file", payload)["file"]
        size = int(fetched["size"])
        if on_progress is not None:
            # Same shape as the upload: the guest copies in one call, and the
            # service reports the ends rather than inventing a middle.
            on_progress(size, size)
        return FileResult(
            path=str(fetched.get("path") or target),
            size=size,
            sha256=str(fetched["sha256"]),
            transport=self.name,
            bytes_transferred=size,
        )

    def close(self, vm: str | None = None) -> None:
        # The persistent session lives in the bridge worker and is closed with it.
        return None
