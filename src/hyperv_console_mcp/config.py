"""Environment-based configuration; secrets are never MCP tool arguments."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .errors import ValidationError

PER_VM_TRANSPORT_PREFIX = "HYPERV_MCP_GUEST_TRANSPORT_"


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


def _local_root(child: str, values: Mapping[str, str] | None = None) -> Path:
    base = (os.environ if values is None else values).get("LOCALAPPDATA", str(Path.home()))
    return Path(base) / "HyperVConsoleMCP" / child


def _path(values: Mapping[str, str], name: str, default: Path) -> Path:
    value = values.get(name, "").strip()
    return Path(value).expanduser() if value else default


def _absolute_path(values: Mapping[str, str], name: str, default: Path) -> Path:
    value = values.get(name, "").strip()
    if not value:
        return default
    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute():
        raise ValidationError(f"{name} must be an absolute path.")
    return path


def _optional_path(values: Mapping[str, str], name: str) -> Path | None:
    value = values.get(name, "").strip()
    if not value:
        return None
    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute():
        raise ValidationError(f"{name} must be an absolute path.")
    return path


def _subnet(values: Mapping[str, str], name: str, default: str) -> str:
    value = values.get(name, default).strip() or default
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        raise ValidationError(f"{name} must be an IPv4 network such as 10.77.0.0/24.") from exc
    if network.version != 4 or network.prefixlen > 30:
        raise ValidationError(f"{name} must be an IPv4 network with room for two hosts.")
    return str(network)


def _transport(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "auto").strip().casefold() or "auto"
    if value not in {"auto", "psdirect", "ssh"}:
        raise ValidationError(f"{name} must be auto, psdirect, or ssh.")
    return value

def _profile(values: Mapping[str, str]) -> str:
    value = values.get("HYPERV_MCP_PROFILE", "safe").strip().casefold() or "safe"
    if value not in {"safe", "lab"}:
        raise ValidationError("HYPERV_MCP_PROFILE must be safe or lab.")
    return value

# The lab profile turns every capability on for the allowlisted VMs. It never
# widens the allowlist itself: an empty list still refuses every mutation.
CAPABILITY_VARIABLES = (
    "HYPERV_MCP_ALLOW_POWER",
    "HYPERV_MCP_ALLOW_CHECKPOINTS",
    "HYPERV_MCP_ALLOW_GUEST",
    "HYPERV_MCP_ALLOW_CONSOLE",
    "HYPERV_MCP_ALLOW_SCREENSHOTS",
    "HYPERV_MCP_ALLOW_DEBUGGER",
    "HYPERV_MCP_ALLOW_MEDIA",
)


@dataclass(frozen=True, slots=True)
class Settings:
    allowed_vms: frozenset[str] = frozenset()
    profile: str = "safe"
    allow_power: bool = False
    allow_checkpoints: bool = False
    allow_guest: bool = False
    allow_console: bool = False
    allow_screenshots: bool = False
    allow_debugger: bool = False
    allow_media: bool = False
    guest_username: str | None = None
    guest_password: str | None = field(default=None, repr=False)
    guest_transport: str = "auto"
    guest_transport_overrides: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 90.0
    max_output_chars: int = 65_536
    audit_log: Path | None = None
    serial_log_root: Path = field(default_factory=lambda: _local_root("serial"))
    iso_root: Path = field(default_factory=lambda: _local_root("media"))
    transfer_root: Path = field(default_factory=lambda: _local_root("transfers"))
    ssh_root: Path = field(default_factory=lambda: _local_root("ssh"))
    ssh_subnet: str = "10.77.0.0/24"
    openssh_package: Path | None = None
    lab_root: Path = field(default_factory=lambda: _local_root("lab") / "packages")
    evidence_root: Path = field(default_factory=lambda: _local_root("evidence"))
    event_directory: Path = field(default_factory=lambda: _local_root("events"))
    event_retention_days: int = 14
    debuggers_dir: Path | None = None
    kd_log_root: Path = field(default_factory=lambda: _local_root("kd"))
    symbol_root: Path = field(default_factory=lambda: _local_root("symbols"))
    kd_timeout_seconds: float = 60.0
    kd_startup_timeout_seconds: float = 60.0

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
        overrides = {
            key[len(PER_VM_TRANSPORT_PREFIX):].casefold().replace("_", "-"): _transport(env, key)
            for key in env
            if key.startswith(PER_VM_TRANSPORT_PREFIX) and len(key) > len(PER_VM_TRANSPORT_PREFIX)
        }
        profile = _profile(env)

        def capability(variable: str) -> bool:
            # The lab profile enables everything for the allowlisted VMs; an
            # explicit "false" still wins so a single capability can be held
            # back without leaving the profile.
            if env.get(variable, "").strip():
                return _boolean(env, variable)
            return profile == "lab"
        return cls(
            allowed_vms=names,
            profile=profile,
            allow_power=capability("HYPERV_MCP_ALLOW_POWER"),
            allow_checkpoints=capability("HYPERV_MCP_ALLOW_CHECKPOINTS"),
            allow_guest=capability("HYPERV_MCP_ALLOW_GUEST"),
            allow_console=capability("HYPERV_MCP_ALLOW_CONSOLE"),
            allow_screenshots=capability("HYPERV_MCP_ALLOW_SCREENSHOTS"),
            allow_debugger=capability("HYPERV_MCP_ALLOW_DEBUGGER"),
            allow_media=capability("HYPERV_MCP_ALLOW_MEDIA"),
            guest_username=env.get("HYPERV_MCP_GUEST_USERNAME") or None,
            guest_password=env.get("HYPERV_MCP_GUEST_PASSWORD") or None,
            guest_transport=_transport(env, "HYPERV_MCP_GUEST_TRANSPORT"),
            guest_transport_overrides=overrides,
            timeout_seconds=float(
                _integer(env, "HYPERV_MCP_TIMEOUT_SECONDS", 90, 1, 900)
            ),
            max_output_chars=_integer(
                env, "HYPERV_MCP_MAX_OUTPUT_CHARS", 65_536, 1_024, 1_048_576
            ),
            audit_log=Path(audit_path).expanduser() if audit_path else None,
            serial_log_root=_path(
                env, "HYPERV_MCP_SERIAL_LOG_ROOT", _local_root("serial", env)
            ),
            iso_root=_path(env, "HYPERV_MCP_ISO_ROOT", _local_root("media", env)),
            transfer_root=_absolute_path(
                env, "HYPERV_MCP_TRANSFER_ROOT", _local_root("transfers", env)
            ),
            ssh_root=_absolute_path(env, "HYPERV_MCP_SSH_ROOT", _local_root("ssh", env)),
            ssh_subnet=_subnet(env, "HYPERV_MCP_SSH_SUBNET", "10.77.0.0/24"),
            openssh_package=_optional_path(env, "HYPERV_MCP_OPENSSH_PACKAGE"),
            lab_root=_absolute_path(
                env, "HYPERV_MCP_LAB_ROOT", _local_root("lab", env) / "packages"
            ),
            evidence_root=_absolute_path(
                env, "HYPERV_MCP_EVIDENCE_ROOT", _local_root("evidence", env)
            ),
            event_directory=_absolute_path(
                env, "HYPERV_MCP_EVENT_DIRECTORY", _local_root("events", env)
            ),
            event_retention_days=_integer(
                env, "HYPERV_MCP_EVENT_RETENTION_DAYS", 14, 1, 3650
            ),
            debuggers_dir=_optional_path(env, "HYPERV_MCP_DEBUGGERS_DIR"),
            kd_log_root=_absolute_path(
                env, "HYPERV_MCP_KD_LOG_ROOT", _local_root("kd", env)
            ),
            symbol_root=_absolute_path(
                env, "HYPERV_MCP_SYMBOL_ROOT", _local_root("symbols", env)
            ),
            kd_timeout_seconds=float(
                _integer(env, "HYPERV_MCP_KD_TIMEOUT_SECONDS", 60, 1, 3600)
            ),
            kd_startup_timeout_seconds=float(
                _integer(env, "HYPERV_MCP_KD_STARTUP_TIMEOUT_SECONDS", 60, 1, 900)
            ),
        )

    def is_allowed(self, vm_name: str) -> bool:
        return any(name.casefold() == vm_name.casefold() for name in self.allowed_vms)

    def guest_transport_for(self, vm_name: str) -> str:
        """Per-VM override from HYPERV_MCP_GUEST_TRANSPORT_<VMNAME>, else the default."""
        key = vm_name.casefold().replace("_", "-")
        return self.guest_transport_overrides.get(key, self.guest_transport)
