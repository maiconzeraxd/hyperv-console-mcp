"""Environment-based configuration; secrets are never MCP tool arguments."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .errors import ValidationError


def _boolean(values: Mapping[str, str], name: str) -> bool:
    value = values.get(name, "false").strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    raise ValidationError(f"{name} must be true or false.")


def _integer(
    values: Mapping[str, str], name: str, default: int, minimum: int, maximum: int
) -> int:
    try:
        result = int(values.get(name, str(default)))
    except ValueError as exc:
        raise ValidationError(f"{name} must be an integer.") from exc
    if not minimum <= result <= maximum:
        raise ValidationError(f"{name} must be between {minimum} and {maximum}.")
    return result


@dataclass(frozen=True, slots=True)
class Settings:
    allowed_vms: frozenset[str] = frozenset()
    allow_power: bool = False
    allow_checkpoints: bool = False
    allow_guest: bool = False
    allow_console: bool = False
    allow_screenshots: bool = False
    allow_debugger: bool = False
    guest_username: str | None = None
    guest_password: str | None = field(default=None, repr=False)
    timeout_seconds: float = 90.0
    max_output_chars: int = 65_536
    audit_log: Path | None = None

    @classmethod
    def from_env(cls, values: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if values is None else values
        names = frozenset(
            name.strip() for name in env.get("HYPERV_MCP_ALLOWED_VMS", "").split(",") if name.strip()
        )
        for name in names:
            if any(ord(character) < 32 for character in name) or len(name) > 256:
                raise ValidationError("HYPERV_MCP_ALLOWED_VMS contains an invalid VM name.")
        audit_path = env.get("HYPERV_MCP_AUDIT_LOG", "").strip()
        return cls(
            allowed_vms=names,
            allow_power=_boolean(env, "HYPERV_MCP_ALLOW_POWER"),
            allow_checkpoints=_boolean(env, "HYPERV_MCP_ALLOW_CHECKPOINTS"),
            allow_guest=_boolean(env, "HYPERV_MCP_ALLOW_GUEST"),
            allow_console=_boolean(env, "HYPERV_MCP_ALLOW_CONSOLE"),
            allow_screenshots=_boolean(env, "HYPERV_MCP_ALLOW_SCREENSHOTS"),
            allow_debugger=_boolean(env, "HYPERV_MCP_ALLOW_DEBUGGER"),
            guest_username=env.get("HYPERV_MCP_GUEST_USERNAME") or None,
            guest_password=env.get("HYPERV_MCP_GUEST_PASSWORD") or None,
            timeout_seconds=float(
                _integer(env, "HYPERV_MCP_TIMEOUT_SECONDS", 90, 1, 900)
            ),
            max_output_chars=_integer(
                env, "HYPERV_MCP_MAX_OUTPUT_CHARS", 65_536, 1_024, 1_048_576
            ),
            audit_log=Path(audit_path).expanduser() if audit_path else None,
        )

    def is_allowed(self, vm_name: str) -> bool:
        return any(name.casefold() == vm_name.casefold() for name in self.allowed_vms)
