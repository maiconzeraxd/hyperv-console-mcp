"""Dispatch host operations to WMI, one operation at a time.

An operation that is not in ``HANDLERS`` is refused with ``wmi_unsupported``,
which is the signal ``FallbackBackend`` uses to send it to the PowerShell
worker. That is what makes the migration incremental: each commit moves one
operation and the rest keep working exactly as they did.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import Any

from ..backend import (
    FallbackBackend,
    GuestSplitBackend,
    HyperVBackend,
    PowerShellBackend,
)
from ..errors import BackendError
from . import checkpoint, console, host, media, power, thumbnail, vm
from .connection import ComConnection, Connection, WmiFailure

UNSUPPORTED = "wmi_unsupported"
UNAVAILABLE = "wmi_unavailable"

Handler = Callable[[Connection, dict[str, Any]], dict[str, Any]]

HANDLERS: dict[str, Handler] = {
    "host_status": host.host_status,
    "vm_list": vm.vm_list,
    "vm_get": vm.vm_get,
    "vm_power": power.vm_power,
    "vm_thumbnail": thumbnail.vm_thumbnail,
    "checkpoint_list": checkpoint.checkpoint_list,
    "checkpoint_restore": checkpoint.checkpoint_restore,
    "checkpoint_delete": checkpoint.checkpoint_delete,
    "media_list": media.media_list,
    "media_mount": media.media_mount,
    "media_eject": media.media_eject,
    "console_info": console.console_info,
    "console_type_text": console.console_type_text,
    "console_press_keys": console.console_press_keys,
    "console_key_state": console.console_key_state,
    "console_ctrl_alt_delete": console.console_ctrl_alt_delete,
    "console_mouse_move": console.console_mouse_move,
    "console_click": console.console_click,
    "console_mouse_buttons": console.console_mouse_buttons,
    "console_scroll": console.console_scroll,
    "console_batch": console.console_batch,
}


class WmiBackend(HyperVBackend):
    """Serves the migrated operations and refuses the rest."""

    def __init__(self, connect: Callable[[], Connection] | None = None) -> None:
        # Lazy on purpose: constructing the backend must never touch COM, so a
        # host without pywin32 fails per call and falls back per call.
        self._connect = connect or ComConnection
        self._thread = threading.local()

    def _connection(self) -> Connection:
        connection = getattr(self._thread, "connection", None)
        if connection is None:
            connection = self._connect()
            self._thread.connection = connection
        return connection

    def invoke(
        self, operation: str, payload: Mapping[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        handler = HANDLERS.get(operation)
        if handler is None:
            raise BackendError(
                f"{operation} is not served by WMI yet; the PowerShell worker handles it.",
                code=UNSUPPORTED,
            )
        try:
            return handler(self._connection(), dict(payload))
        except WmiFailure as exc:
            raise BackendError(str(exc), code=exc.code) from exc

    def close(self) -> None:
        """COM objects die with their apartment; nothing is held open."""


def create_backend(
    script_path: Any = None, executable: str = "powershell.exe"
) -> GuestSplitBackend:
    """Host work through WMI where it is migrated, PowerShell otherwise."""
    return GuestSplitBackend(
        host=FallbackBackend(WmiBackend(), PowerShellBackend(script_path, executable)),
        guest=PowerShellBackend(script_path, executable),
    )
