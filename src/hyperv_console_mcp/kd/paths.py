"""Where the debugger keeps its state on the host, one directory per VM."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
MAX_NAME = 96


def safe_name(vm_name: str) -> str:
    """A directory name that cannot escape the root or collide with a path."""
    cleaned = _UNSAFE.sub("_", vm_name).strip("._-")
    return (cleaned or "vm")[:MAX_NAME]


def vm_directory(settings: Any, vm_name: str) -> Path:
    return Path(settings.kd_log_root) / safe_name(vm_name)


def session_log_path(settings: Any, vm_name: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return vm_directory(settings, vm_name) / f"{stamp}.log"


def kdnet_state_path(settings: Any, vm_name: str) -> Path:
    return vm_directory(settings, vm_name) / "kdnet.json"


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_json(path: Path, value: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


__all__ = [
    "kdnet_state_path",
    "read_json",
    "safe_name",
    "session_log_path",
    "vm_directory",
    "write_json",
]
