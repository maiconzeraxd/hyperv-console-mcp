"""PowerShell Direct transport: the Hyper-V bridge's guest operations."""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

from ..errors import PolicyError, ValidationError
from .base import MAX_INLINE_BYTES, ExecResult, FileResult

CHUNK_BYTES = 4 * 1024 * 1024

# Structured guest operations that run the same guest-side PowerShell over any
# transport; the transport only decides how the request reaches the guest.
GUEST_OPERATIONS = frozenset({
    "guest_processes", "guest_services", "guest_events",
    "guest_read_file", "guest_read_chunk", "guest_assemble_file",
    "guest_write_file", "guest_publish_package",
})


class PowerShellDirectTransport:
    """Guest access over the persistent bridge worker's `New-PSSession -VMName`."""

    name = "psdirect"

    def __init__(self, backend: Any, settings: Any) -> None:
        self.backend = backend
        self.settings = settings

    def _credentials(self, vm: str) -> dict[str, str]:
        if not self.settings.guest_username or not self.settings.guest_password:
            raise PolicyError(
                "PowerShell Direct requires HYPERV_MCP_GUEST_USERNAME and "
                "HYPERV_MCP_GUEST_PASSWORD in the server environment."
            )
        return {
            "vm": vm,
            "username": self.settings.guest_username,
            "password": self.settings.guest_password,
        }

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
        data = Path(local).read_bytes()
        if len(data) > MAX_INLINE_BYTES:
            raise ValidationError(
                f"{Path(local).name} is larger than the {MAX_INLINE_BYTES} byte inline limit."
            )
        payload = self._credentials(vm)
        payload.update(
            path=remote,
            content_base64=base64.b64encode(data).decode("ascii"),
            overwrite=overwrite,
            create_parent=True,
        )
        written = self._invoke("guest_write_file", payload)["file"]
        digest = hashlib.sha256(data).hexdigest()
        if str(written.get("sha256", "")).casefold() != digest:
            raise ValidationError("The guest reported a different SHA-256 than the host wrote.")
        return FileResult(
            path=remote, size=len(data), sha256=digest, transport=self.name,
            bytes_transferred=len(data),
        )

    def get_file(self, vm: str, remote: str, local: Path) -> FileResult:
        info = self.file_info(vm, remote)
        size = int(info["size"])
        target = Path(local)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        handle, temporary = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".part", dir=str(target.parent)
        )
        transferred = 0
        try:
            with os.fdopen(handle, "wb") as stream:
                while transferred < size:
                    count = min(CHUNK_BYTES, size - transferred)
                    payload = self._credentials(vm)
                    payload.update(path=remote, offset=transferred, count=count)
                    chunk = self._invoke("guest_read_chunk", payload)["chunk"]
                    data = base64.b64decode(chunk["content_base64"], validate=True)
                    if len(data) != count:
                        raise ValidationError("The guest returned a short chunk.")
                    if hashlib.sha256(data).hexdigest() != str(chunk["sha256"]).casefold():
                        raise ValidationError("A guest chunk failed its SHA-256 check.")
                    stream.write(data)
                    digest.update(data)
                    transferred += count
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return FileResult(
            path=remote, size=size, sha256=digest.hexdigest(), transport=self.name,
            bytes_transferred=transferred,
        )

    def close(self, vm: str | None = None) -> None:
        # The persistent session lives in the bridge worker and is closed with it.
        return None
