"""Per-VM access levels, and the file the operator writes them in.

The allowlist and the seven ``HYPERV_MCP_ALLOW_*`` flags said the same thing
twice, once per VM and once per capability, and every capability was host-wide:
there was no way to let the AI drive one VM and only read another. A level
names the same policy in one word, per VM:

``read-only``
    The VM is listed and its state read; nothing changes.
``control``
    Power, checkpoints, console input, screenshots and media. The guest is
    still not reachable: no command runs inside it.
``lab``
    Everything ``control`` has, plus guest access and the debugger — the level
    the driver lab needs.

The file lives under ``%LOCALAPPDATA%\\HyperVConsoleMCP\\access.json`` and is
written only by this module's CLI (``hyperv-access``) or the dashboard, both
run by the operator. No MCP tool writes it: a policy the AI could raise is not
a policy. Environment variables still win where they are set, because they are
explicit and per-process — which is what a test or a one-off run wants.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ValidationError

CAPABILITIES = (
    "power", "checkpoints", "guest", "console", "screenshots", "debugger", "media",
)

# Guest access and the debugger are what separate lab from control: both put
# code or a debugger inside the guest, which is the line the project keeps.
LEVEL_CAPABILITIES: dict[str, frozenset[str]] = {
    "read-only": frozenset(),
    "control": frozenset({"power", "checkpoints", "console", "screenshots", "media"}),
    "lab": frozenset(CAPABILITIES),
}
LEVELS = tuple(LEVEL_CAPABILITIES)

FILE_VERSION = 1


def access_file(values: Mapping[str, str] | None = None) -> Path:
    """Where the operator's levels live, unless ``HYPERV_MCP_ACCESS_FILE`` says."""
    env = os.environ if values is None else values
    override = env.get("HYPERV_MCP_ACCESS_FILE", "").strip()
    if override:
        path = Path(os.path.expandvars(override)).expanduser()
        if not path.is_absolute():
            raise ValidationError("HYPERV_MCP_ACCESS_FILE must be an absolute path.")
        return path
    base = env.get("LOCALAPPDATA", str(Path.home()))
    return Path(base) / "HyperVConsoleMCP" / "access.json"


def normalise_level(value: Any) -> str:
    level = str(value or "").strip().casefold()
    if level not in LEVEL_CAPABILITIES:
        raise ValidationError(
            f"An access level must be one of {', '.join(LEVELS)}; got {value!r}."
        )
    return level


def _key(vm_name: str) -> str:
    name = str(vm_name or "").strip()
    if not name or len(name) > 256 or any(ord(character) < 32 for character in name):
        raise ValidationError("A VM name must be 1-256 characters without control characters.")
    return name.casefold()


@dataclass(frozen=True, slots=True)
class AccessStore:
    """The operator's policy file: one level per VM, keyed case-insensitively."""

    path: Path

    def load(self) -> dict[str, tuple[str, str]]:
        """``casefolded name -> (name as written, level)``; empty when absent.

        A file that exists but cannot be read or parsed is an error rather than
        an empty policy: silently starting with no VMs would look like a broken
        host, and silently starting with everything would ignore the operator.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise ValidationError(
                f"The access file {self.path} could not be read: {exc}"
            ) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("vms", {}), dict):
            raise ValidationError(f"The access file {self.path} must hold an object with a 'vms' map.")
        entries: dict[str, tuple[str, str]] = {}
        for key, entry in (raw.get("vms") or {}).items():
            name, level = (entry.get("name"), entry.get("level")) if isinstance(entry, dict) else (key, entry)
            name = str(name or key)
            entries[_key(name)] = (name.strip(), normalise_level(level))
        return entries

    def levels(self) -> dict[str, str]:
        """``casefolded name -> level``, which is all the policy needs."""
        return {key: level for key, (_name, level) in self.load().items()}

    def set(self, vm_name: str, level: str) -> dict[str, tuple[str, str]]:
        entries = self.load()
        entries[_key(vm_name)] = (str(vm_name).strip(), normalise_level(level))
        return self._save(entries)

    def remove(self, vm_name: str) -> dict[str, tuple[str, str]]:
        entries = self.load()
        entries.pop(_key(vm_name), None)
        return self._save(entries)

    def _save(self, entries: dict[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
        payload = {
            "version": FILE_VERSION,
            "vms": {
                key: {"name": name, "level": level}
                for key, (name, level) in sorted(entries.items())
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Written beside the target and renamed: a reader never sees half a file,
        # and a crash mid-write leaves the previous policy in place.
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
        return entries
