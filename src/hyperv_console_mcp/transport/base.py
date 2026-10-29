"""Transport-neutral guest access: PowerShell Direct today, SSH optionally later."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# The bridge sends file content inline as base64 inside one JSON request, so a
# single write is limited to the service's 8 MiB cap.
MAX_INLINE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ExecResult:
    success: bool
    exit_code: int
    output: str
    had_errors: bool
    error_records: tuple[str, ...]
    truncated: bool
    transport: str

    def as_dict(self, **extra: Any) -> dict[str, Any]:
        return {
            "success": self.success,
            "exit_code": self.exit_code,
            "output": self.output,
            "had_errors": self.had_errors,
            "error_records": list(self.error_records),
            "output_truncated": self.truncated,
            "transport": self.transport,
            **extra,
        }


@dataclass(frozen=True, slots=True)
class FileResult:
    path: str
    size: int
    sha256: str
    transport: str
    bytes_transferred: int | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path, "size": self.size,
            "sha256": self.sha256, "transport": self.transport,
        }
        if self.bytes_transferred is not None:
            payload["bytes_transferred"] = self.bytes_transferred
        return payload


@runtime_checkable
class GuestTransport(Protocol):
    """The guest operations every transport provides.

    `call` is the structured extension point: the guest inventory tools
    (processes, services, events, file metadata) run the same guest-side
    PowerShell over both transports, so a transport implements them by
    delivering that request and returning its JSON. Requests never carry
    credentials in results.
    """

    name: str

    def exec(self, vm: str, command: str, *, timeout: float, max_output: int) -> ExecResult: ...

    def put_file(self, vm: str, local: Path, remote: str, *, overwrite: bool) -> FileResult: ...

    def get_file(self, vm: str, remote: str, local: Path) -> FileResult: ...

    def health(self, vm: str) -> dict[str, Any]: ...

    def call(self, vm: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    def close(self, vm: str | None = None) -> None: ...
