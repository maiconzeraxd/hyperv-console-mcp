"""The programs a frozen build ships, and how a running one finds its siblings.

A PyInstaller build is one folder with several entry points in it. Three places
have to name one of them — the client entry an install writes, the shortcut it
creates, and the host a client starts — and under an interpreter all three are
``python -m …`` instead. The names live here once, so the build spec, the
installer and the code cannot disagree about what a file is called.

Nothing else changes when frozen: the launcher still decides by stdin, the host
still owns the pipe, and the install still writes the same client entry.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The window and the server: one entry point, as on an interpreter.
LAUNCHER = "HyperVConsoleMCP.exe"
# The operator tools, shipped beside it so an error message that names one of
# them — `hyperv-access set …` — is a command the operator actually has.
SETUP = "hyperv-setup.exe"
ACCESS = "hyperv-access.exe"
HOST = "hyperv-service.exe"


def is_frozen() -> bool:
    """Whether this is a PyInstaller build rather than an interpreter."""
    return bool(getattr(sys, "frozen", False))


def sibling(name: str) -> Path:
    """A program shipped in the same folder as this one."""
    return Path(sys.executable).with_name(name)
