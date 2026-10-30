"""The operator's per-VM transport choice, kept between windows.

Which path carries a guest command is a preference, not a policy: PowerShell
Direct needs nothing installed and works before the guest has a network, SSH is
faster once it is provisioned. ``auto`` already picks between them, so this file
exists for the case where the operator knows better than the probe — a guest
whose sshd is up but slow, or a run that must not touch the network at all.

It is written by the window's transport switch and by ``guest_transport_set``,
which is the same service method the AI calls. It is read on every selection,
so a change applies to the next call without restarting anything. Environment
variables still win where they are set, because they are explicit and
per-process — which is what a test or a one-off run wants.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from ..errors import ValidationError

CHOICES = ("auto", "psdirect", "ssh")
FILE_VERSION = 1


def choice_file(values: Mapping[str, str] | None = None) -> Path:
    """Where the saved choices live, unless ``HYPERV_MCP_TRANSPORT_FILE`` says."""
    env = os.environ if values is None else values
    override = env.get("HYPERV_MCP_TRANSPORT_FILE", "").strip()
    if override:
        path = Path(os.path.expandvars(override)).expanduser()
        if not path.is_absolute():
            raise ValidationError("HYPERV_MCP_TRANSPORT_FILE must be an absolute path.")
        return path
    base = env.get("LOCALAPPDATA", str(Path.home()))
    return Path(base) / "HyperVConsoleMCP" / "transport.json"


def normalise_transport(value: Any) -> str:
    choice = str(value or "").strip().casefold()
    if choice not in CHOICES:
        raise ValidationError(
            "transport must be auto, psdirect, or ssh."
        )
    return choice


def _key(vm_name: str) -> str:
    return str(vm_name).strip().casefold()


class TransportChoiceStore:
    """One saved transport per VM, keyed case-insensitively.

    A file that cannot be read is an error rather than an empty choice: silently
    going back to ``auto`` would look like the probe disagreed with the operator,
    and the switch in the window would appear to forget what it was told.
    """

    path: Path

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def choices(self) -> dict[str, str]:
        """``casefolded VM name -> transport``; empty when the file is absent."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise ValidationError(
                f"The transport file {self.path} could not be read: {exc}"
            ) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("vms", {}), dict):
            raise ValidationError(
                f"The transport file {self.path} must hold an object with a 'vms' map."
            )
        choices: dict[str, str] = {}
        for key, entry in (raw.get("vms") or {}).items():
            value = entry.get("transport") if isinstance(entry, dict) else entry
            try:
                choices[_key(key)] = normalise_transport(value)
            except ValidationError:
                # One bad row is a typo, not a reason to lose the others.
                continue
        return choices

    def get(self, vm_name: str) -> str | None:
        return self.choices().get(_key(vm_name))

    def set(self, vm_name: str, transport: str) -> dict[str, str]:
        choices = self.choices()
        choices[_key(vm_name)] = normalise_transport(transport)
        return self._save(choices)

    def remove(self, vm_name: str) -> dict[str, str]:
        choices = self.choices()
        choices.pop(_key(vm_name), None)
        return self._save(choices)

    def _save(self, choices: dict[str, str]) -> dict[str, str]:
        payload = {
            "version": FILE_VERSION,
            "vms": {
                key: {"transport": choice} for key, choice in sorted(choices.items())
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Written beside the target and renamed, like the access file: a reader
        # never sees half a file, and a crash mid-write keeps the previous choice.
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self.path)
        return choices


__all__ = [
    "CHOICES", "FILE_VERSION", "TransportChoiceStore", "choice_file", "normalise_transport",
]
