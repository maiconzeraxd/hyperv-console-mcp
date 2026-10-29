"""Locate the Windows SDK debugger: ``kd.exe`` (kernel) or ``cdb.exe`` (user mode).

Only the classic command-line debuggers are usable for automation. WinDbgX
(``WinDbgX.exe``, the Microsoft Store app) has no stable stdio command
protocol, so it is never a candidate.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from ..errors import DebuggerError

DEBUGGER_NAMES = ("kd", "cdb")
# The Debugging Tools for Windows live under the Windows SDK. Both the 10.x
# and the newer 11.x layout are checked; the x64 build is the one that can
# debug x64 kernels and is what Hyper-V guests need.
DEFAULT_DIRECTORIES = (
    Path(r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64"),
    Path(r"C:\Program Files\Windows Kits\10\Debuggers\x64"),
    Path(r"C:\Program Files (x86)\Windows Kits\11\Debuggers\x64"),
    Path(r"C:\Program Files\Windows Kits\11\Debuggers\x64"),
)


def _executable(directory: Path, name: str) -> Path | None:
    candidate = directory / f"{name}.exe"
    return candidate if candidate.is_file() else None


def find_debugger(
    name: str = "kd", *, directory: Path | str | None = None
) -> Path:
    """Return the absolute path of ``kd.exe`` or ``cdb.exe``.

    ``directory`` (HYPERV_MCP_DEBUGGERS_DIR) wins over PATH and the SDK
    defaults, so a portable install can be pointed at explicitly.
    """
    if name not in DEBUGGER_NAMES:
        raise DebuggerError(f"Unknown debugger {name!r}; expected kd or cdb.", code="kd_not_found")
    searched: list[str] = []
    if directory is not None:
        folder = Path(os.path.expandvars(str(directory))).expanduser()
        searched.append(str(folder))
        found = _executable(folder, name)
        if found is not None:
            return found.resolve()
    on_path = shutil.which(f"{name}.exe") or shutil.which(name)
    if on_path:
        return Path(on_path).resolve()
    for folder in DEFAULT_DIRECTORIES:
        searched.append(str(folder))
        found = _executable(folder, name)
        if found is not None:
            return found.resolve()
    raise DebuggerError(
        f"Could not find {name}.exe. Install the Windows SDK 'Debugging Tools for "
        "Windows' (x64) or set HYPERV_MCP_DEBUGGERS_DIR to its Debuggers\\x64 "
        f"folder. Searched: {', '.join(searched) or 'PATH only'}.",
        code="kd_not_found",
    )


__all__ = ["DEFAULT_DIRECTORIES", "DEBUGGER_NAMES", "find_debugger"]
