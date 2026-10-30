"""Environment-based configuration; secrets are never MCP tool arguments."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .access import CAPABILITIES, LEVEL_CAPABILITIES, AccessStore, access_file
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


def local_root(child: str) -> Path:
    """The app's own directory for one kind of file, under ``%LOCALAPPDATA%``."""
    return _local_root(child)


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


# Every tool belongs to exactly one family. The server registers only the
# families the profile enables, because every schema a conversation never uses
# is a cost the model pays anyway.
TOOLSETS = frozenset({
    "core", "guest", "console", "config", "credentials", "media", "serial", "ssh",
    "lab", "kd", "debug",
})

# A profile is a starting point, not a ceiling: HYPERV_MCP_TOOLSETS can add or
# drop any family. Both defaults stay within the 40-tool budget — safe is the
# everyday surface, lab adds the package cycle, the guest configuration that
# cycle depends on, and the debugger.
DEFAULT_TOOLSETS: dict[str, frozenset[str]] = {
    "safe": frozenset({"core", "guest", "console", "config", "credentials", "media", "ssh"}),
    "lab": frozenset({"core", "guest", "console", "config", "lab", "kd", "debug"}),
}


def _toolsets(values: Mapping[str, str], profile: str) -> frozenset[str]:
    raw = values.get("HYPERV_MCP_TOOLSETS", "").strip()
    if not raw:
        return DEFAULT_TOOLSETS[profile]
    names = frozenset(name.strip().casefold() for name in raw.split(",") if name.strip())
    if "all" in names:
        return TOOLSETS
    unknown = names - TOOLSETS
    if unknown:
        raise ValidationError(
            "HYPERV_MCP_TOOLSETS contains unknown toolsets: "
            + ", ".join(sorted(unknown))
            + f". Known: {', '.join(sorted(TOOLSETS))}."
        )
    if not names:
        raise ValidationError("HYPERV_MCP_TOOLSETS must name at least one toolset.")
    return names


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
    # casefolded VM name -> access level, from the operator's access file. A VM
    # with a level takes its capabilities from that level; the flags below are
    # the fallback for a VM with none, which is how a bare environment behaves.
    levels: Mapping[str, str] = field(default_factory=dict)
    # casefolded VM name -> the checkpoint the operator protects. The baseline is
    # the state the guest is expected to return to, so the service refuses to
    # delete it or to let a new checkpoint take its name — for every actor.
    baselines: Mapping[str, str] = field(default_factory=dict)
    access_path: Path | None = None
    profile: str = "safe"
    toolsets: frozenset[str] = DEFAULT_TOOLSETS["safe"]
    # One background host owns the workers, the sessions and the events, so the
    # window can see what the AI is doing. A client that sets this false runs its
    # own copy instead, which is what a test or a one-off script wants.
    shared_service: bool = True
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
        path = access_file(env)
        entries = AccessStore(path).entries()
        names = frozenset(
            name.strip() for name in env.get("HYPERV_MCP_ALLOWED_VMS", "").split(",") if name.strip()
        )
        for name in names:
            if any(ord(character) < 32 for character in name) or len(name) > 256:
                raise ValidationError("HYPERV_MCP_ALLOWED_VMS contains an invalid VM name.")
        # An explicit allowlist narrows the file rather than being replaced by
        # it: a test or a one-off run names the VMs it means, and the file still
        # supplies each VM's level.
        if not names:
            names = frozenset(str(record["name"]) for record in entries.values())
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
            levels={key: str(record["level"]) for key, record in entries.items()},
            baselines={
                key: str(record["baseline"])
                for key, record in entries.items()
                if record["baseline"]
            },
            access_path=path,
            profile=profile,
            toolsets=_toolsets(env, profile),
            shared_service=(
                _boolean(env, "HYPERV_MCP_SHARED_SERVICE")
                if env.get("HYPERV_MCP_SHARED_SERVICE", "").strip()
                else True
            ),
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

    def level_for(self, vm_name: str) -> str | None:
        """The access level the operator gave this VM, if any."""
        return self.levels.get(vm_name.strip().casefold())

    def baseline_for(self, vm_name: str) -> str | None:
        """The checkpoint this VM must not lose, if the operator named one."""
        return self.baselines.get(vm_name.strip().casefold())

    def capabilities_for(self, vm_name: str) -> frozenset[str]:
        """What this VM may do: its level's set, or the global flags.

        A level is per VM and therefore more specific than a host-wide flag, so
        it wins. A VM with no level keeps the older behaviour, where the profile
        and the ``HYPERV_MCP_ALLOW_*`` variables decide for every VM at once.
        """
        level = self.level_for(vm_name)
        if level is not None:
            return LEVEL_CAPABILITIES[level]
        flags = (
            ("power", self.allow_power),
            ("checkpoints", self.allow_checkpoints),
            ("guest", self.allow_guest),
            ("console", self.allow_console),
            ("screenshots", self.allow_screenshots),
            ("debugger", self.allow_debugger),
            ("media", self.allow_media),
        )
        return frozenset(name for name, enabled in flags if enabled)

    def allows(self, vm_name: str, capability: str) -> bool:
        if capability not in CAPABILITIES:
            raise ValidationError(f"Unknown capability {capability!r}.")
        return capability in self.capabilities_for(vm_name)

    def refusal(self, vm_name: str, capability: str) -> str:
        """Why this VM may not do this, in the words of whatever decided it."""
        level = self.level_for(vm_name)
        if level is not None:
            return (
                f"VM {vm_name!r} has access level {level!r}, which does not include {capability}. "
                f"Raise it with: hyperv-access set \"{vm_name}\" control|lab"
            )
        return (
            f"This capability is disabled. Set HYPERV_MCP_ALLOW_{capability.upper()}=true "
            f"to enable it, or give {vm_name!r} an access level: "
            f"hyperv-access set \"{vm_name}\" control|lab"
        )

    def guest_transport_for(self, vm_name: str) -> str:
        """Per-VM override from HYPERV_MCP_GUEST_TRANSPORT_<VMNAME>, else the default."""
        key = vm_name.casefold().replace("_", "-")
        return self.guest_transport_overrides.get(key, self.guest_transport)
