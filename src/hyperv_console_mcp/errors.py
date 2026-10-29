"""Errors that are safe to surface as MCP tool failures."""

from __future__ import annotations

from typing import Any

# A fix answers "what would make this work?" without the caller having to
# guess. Two shapes are allowed, and they are not interchangeable:
#   {"tool": <registered tool name>, "args": {...}}  the caller can do this now
#   {"operator": <what a human must do>}             nobody but the operator can
# A tool a fix names is checked against the registered surface in the tests,
# because a fix that calls a tool nobody registered is worse than no fix.


class HyperVError(Exception):
    """Base error with a stable machine-readable code, and a fix when one exists."""

    def __init__(
        self, message: str, *, code: str = "hyperv_error", fix: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.fix = fix


class ValidationError(HyperVError):
    def __init__(self, message: str, *, fix: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="invalid_argument", fix=fix)


class PolicyError(HyperVError):
    def __init__(self, message: str, *, fix: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="not_authorized", fix=fix)


class BackendError(HyperVError):
    def __init__(
        self, message: str, *, code: str = "backend_error", fix: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message, code=code, fix=fix)


class CaptureError(HyperVError):
    def __init__(self, message: str, *, fix: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="console_capture_failed", fix=fix)


class DebuggerError(HyperVError):
    """A kd.exe session failed: no debugger, no target, a lost marker, or an exit."""

    def __init__(
        self, message: str, *, code: str = "kd_error", fix: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message, code=code, fix=fix)
