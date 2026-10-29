"""The VM's COM ports and the debugger tools this host has.

A serial port is an ``Msvm_SerialPortSettingData`` of resource type 21, named
``Microsoft:<vm>\\<controller>\\<number>`` where the last number is zero-based;
the pipe or device it is connected to is ``Connection[0]`` and ``DebuggerMode``
is the flag ``Get-VMComPort`` shows as On or Off.

The class holds every VM's ports, so both operations scope the query by the
VM's own identifier. A checkpoint's copy of a port is keyed by the checkpoint's
identifier instead — ``Microsoft:<checkpoint>\\<controller>\\<number>`` — so the
same prefix keeps those out as well.

The tool probe is the one part of this operation that is not WMI: it is two
paths on the host, which is cheaper to answer here than to ask the worker.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .connection import Connection, WmiFailure
from .jobs import await_job
from .vm import VM_STATES, _int, find_vm

KDNET_PATH = Path(r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\kdnet.exe")
_SERIAL_RESOURCE_TYPE = 21
_OFF = 3
_BAUD_RATE = 115200
# Anchored, unlike the bridge's ``-notmatch``: that one only asks whether the
# string *contains* a valid name, so ``!!!abc`` would have been accepted.
_PIPE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_NEXT_STEPS = [
    "Configure guest BCD debugging separately while the VM is Running, if it is not already enabled.",
    "Start WinDbg with the returned arguments before starting the VM to catch early boot.",
    "Do not enable Enhanced Session mode while stopped at a kernel breakpoint.",
    "Checkpoint before driver testing; kernel debugging does not prevent a bugcheck.",
]


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


def debug_serial_prepare(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    """Point COM1 at a named pipe and turn debugger mode on.

    Hyper-V refuses a port change unless the VM is off, and the tool will not
    stop it: a VM stopped here could lose the very session the caller is
    debugging. The result is read back rather than assumed — the provider
    answers a modify it did not apply with a success code on some settings.
    """
    vm = find_vm(connection, str(payload.get("vm") or ""))
    if _int(vm.get("EnabledState")) != _OFF:
        raise WmiFailure(
            "The VM must be Off before configuring COM1. "
            "The server will not stop it automatically.",
            code="invalid_state",
        )
    pipe_name = str(payload.get("pipe_name") or "")
    if _PIPE_NAME.fullmatch(pipe_name) is None:
        raise WmiFailure(
            "The named pipe must contain only letters, digits, dot, underscore, or hyphen.",
            code="invalid_argument",
        )
    identifier = str(vm.get("Name") or "")
    pipe_path = "\\\\.\\pipe\\" + pipe_name
    port = _port_setting(connection, identifier, 0)
    response = connection.call(
        _service(connection), "ModifyResourceSettings", ResourceSettings=[_port_xml(port, pipe_path)]
    )
    await_job(connection, response)
    _verify(connection, identifier, pipe_path)
    return {
        "vm": vm.get("ElementName"),
        "port_number": 1,
        "pipe_path": pipe_path,
        "debugger_mode": "On",
        "baud_rate": _BAUD_RATE,
        "windbg_arguments": (
            f"-k com:pipe,port={pipe_path},baud={_BAUD_RATE},reconnect,resets=0"
        ),
        "next_steps": list(_NEXT_STEPS),
    }


def _port_setting(connection: Connection, identifier: str, index: int) -> dict[str, Any]:
    """The realized setting of one port, by its zero-based index."""
    rows = connection.query(
        "SELECT InstanceID, ElementName, Connection, DebuggerMode "
        "FROM Msvm_SerialPortSettingData "
        f"WHERE InstanceID LIKE '{_literal('Microsoft:' + identifier)}%' "
        f"AND ResourceType = {_SERIAL_RESOURCE_TYPE}"
    )
    matches = [row for row in rows if _slot(row) == index]
    if len(matches) != 1:
        raise WmiFailure(
            f"Expected one serial port at index {index} on '{identifier}'; found {len(matches)}.",
            code="not_found",
        )
    return matches[0]


def _port_xml(port: dict[str, Any], path: str) -> str:
    """The port as an embedded instance, keyed by its InstanceID.

    ``Connection`` is an array property, so it needs the ``PROPERTY.ARRAY``
    wrapper; without it the provider reads the path as a scalar and refuses.
    Only the properties being changed are sent, because the rest of the setting
    carries text the host has already translated.
    """
    return (
        '<INSTANCE CLASSNAME="Msvm_SerialPortSettingData">'
        f'<PROPERTY NAME="InstanceID" TYPE="string"><VALUE>{escape(str(port.get("InstanceID") or ""))}</VALUE></PROPERTY>'
        f'<PROPERTY NAME="ResourceType" TYPE="uint16"><VALUE>{_SERIAL_RESOURCE_TYPE}</VALUE></PROPERTY>'
        '<PROPERTY.ARRAY NAME="Connection" TYPE="string"><VALUE.ARRAY>'
        f"<VALUE>{escape(path)}</VALUE>"
        "</VALUE.ARRAY></PROPERTY.ARRAY>"
        '<PROPERTY NAME="DebuggerMode" TYPE="boolean"><VALUE>TRUE</VALUE></PROPERTY>'
        "</INSTANCE>"
    )


def _verify(connection: Connection, identifier: str, path: str) -> None:
    """COM1 must now be on that pipe, in debugger mode — or the call did nothing."""
    first = next((port for port in _ports(connection, identifier) if port["number"] == 1), None)
    if first is None or first["path"] != path or first["debugger_mode"] != "On":
        raise WmiFailure(
            f"COM1 was not changed: it reports {first!r} instead of '{path}' in debugger mode.",
            code="backend_error",
        )


def _service(connection: Connection) -> str:
    rows = connection.query("SELECT * FROM Msvm_VirtualSystemManagementService")
    if len(rows) != 1:
        raise WmiFailure(
            f"Expected one management service on this host; found {len(rows)}.",
            code="wmi_unavailable",
        )
    return str(rows[0]["__PATH"])


def _literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "''")
