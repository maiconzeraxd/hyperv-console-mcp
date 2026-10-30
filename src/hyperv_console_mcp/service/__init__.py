"""The service façade: one object, one policy boundary, one audit trail.

The implementation is split by domain — ``vm``, ``guest``, ``files``, ``ssh``,
``credentials``, ``guest_config``, ``lab``, ``console``, ``media``,
``serial_console``, ``debug_setup`` and ``debugger`` — and composed here.
Callers keep importing ``HyperVService`` from this package, so the split moves
where the code lives and changes nothing about what it does.
"""

from __future__ import annotations

import time  # noqa: F401 — re-exported so a caller can freeze the polling clock.

from ..kd import find_debugger  # noqa: F401 — named here, resolved per call below.
from ..kd.session import KdSession, run_dump  # noqa: F401 — same single naming point.
from ._base import (
    BUTTONS,
    COMMAND_PREVIEW_LINES,
    GATED_TOOLS,
    KD_SYMBOL_SERVER,
    KEY_NAMES,
    MAX_GUEST_FILE_BYTES,
    WAIT_CHANGE_THRESHOLD,
    WAIT_POLL_SECONDS,
    WAIT_SAMPLE_SIZE,
    WAIT_SAMPLE_WIDTH,
    ServiceBase,
)
from .console import ConsoleService
from .credentials import CredentialService
from .debug_setup import DebugSetupService
from .debugger import DebuggerService
from .files import FileService
from .guest import GuestService
from .guest_config import GuestConfigService
from .lab import LabService
from .media import MediaService
from .serial_console import SerialConsoleService
from .ssh import SshService
from .vm import VmService

__all__ = [
    "BUTTONS",
    "COMMAND_PREVIEW_LINES",
    "GATED_TOOLS",
    "HyperVService",
    "KD_SYMBOL_SERVER",
    "KEY_NAMES",
    "MAX_GUEST_FILE_BYTES",
    "WAIT_CHANGE_THRESHOLD",
    "WAIT_POLL_SECONDS",
    "WAIT_SAMPLE_SIZE",
    "WAIT_SAMPLE_WIDTH",
]


class HyperVService(
    VmService,
    GuestService,
    FileService,
    SshService,
    CredentialService,
    GuestConfigService,
    LabService,
    ConsoleService,
    MediaService,
    SerialConsoleService,
    DebugSetupService,
    DebuggerService,
    ServiceBase,
):
    """Validation and authorization, independent of the live Hyper-V backend.

    Every domain module holds the methods for one part of the surface; this
    class is only the composition, so ``HyperVService`` keeps the single public
    shape that the MCP server, the dashboard and the lab runner call.
    """
