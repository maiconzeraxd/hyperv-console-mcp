"""The VM's COM ports and the debugger tools this host has.

A serial port is an ``Msvm_SerialPortSettingData`` of resource type 21, named
``Microsoft:<vm>\\<controller>\\<number>`` where the last number is zero-based;
the pipe or device it is connected to is ``Connection[0]`` and ``DebuggerMode``
is the flag ``Get-VMComPort`` shows as On or Off.

The tool probe is the one part of this operation that is not WMI: it is two
paths on the host, which is cheaper to answer here than to ask the worker.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .connection import Connection
from .vm import VM_STATES, _int, find_vm

KDNET_PATH = Path(r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\kdnet.exe")
_SERIAL_RESOURCE_TYPE = 21


def debug_serial_status(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = find_vm(connection, str(payload.get("vm") or ""))
    identifier = str(vm.get("Name") or "")
    return {
        "vm": vm.get("ElementName"),
        "state": VM_STATES.get(_int(vm.get("EnabledState")), "Other"),
        "com_ports": _ports(connection, identifier),
        "tools": {
            "windbgx": shutil.which("WinDbgX.exe") is not None,
            "kdnet": str(KDNET_PATH),
            "kdnet_installed": KDNET_PATH.is_file(),
        },
    }


def _ports(connection: Connection, identifier: str) -> list[dict[str, Any]]:
    rows = connection.query(
        "SELECT InstanceID, ElementName, Connection, DebuggerMode "
        "FROM Msvm_SerialPortSettingData "
        f"WHERE InstanceID LIKE '{_literal('Microsoft:' + identifier)}%' "
        f"AND ResourceType = {_SERIAL_RESOURCE_TYPE}"
    )
    return sorted((_port(row) for row in rows), key=lambda port: port["number"])


def _port(row: dict[str, Any]) -> dict[str, Any]:
    """One port, with the number the tool calls it by rather than the zero index.

    An unconnected port keeps the empty string the bridge reports; it is not
    ``None``, and a caller comparing the two answers would see the difference.
    """
    connection = row.get("Connection") or []
    return {
        "number": _slot(row) + 1,
        "path": str(connection[0]) if connection else "",
        "debugger_mode": "On" if row.get("DebuggerMode") else "Off",
    }


def _slot(row: dict[str, Any]) -> int:
    """The zero-based port index, which is the last part of the InstanceID."""
    parts = str(row.get("InstanceID") or "").split("\\")
    try:
        return int(parts[-1])
    except (IndexError, ValueError):
        return 0


def _literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "''")
