"""Errors that are safe to surface as MCP tool failures."""


class HyperVError(Exception):
    """Base error with a stable machine-readable code."""

    def __init__(self, message: str, *, code: str = "hyperv_error") -> None:
        super().__init__(message)
        self.code = code


class ValidationError(HyperVError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="invalid_argument")


class PolicyError(HyperVError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="not_authorized")


class BackendError(HyperVError):
    def __init__(self, message: str, *, code: str = "backend_error") -> None:
        super().__init__(message, code=code)


class CaptureError(HyperVError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="console_capture_failed")
