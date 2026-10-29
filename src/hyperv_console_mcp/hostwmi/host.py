"""Host facts, read straight from CIM instead of through the bridge.

The bridge answered ``host_status`` with ``Get-VMHost`` plus two CIM classes;
this reads the same two classes directly, so the result keeps its shape and the
call stops paying for a PowerShell startup, a module import and a JSON round
trip through a worker.
"""

from __future__ import annotations

from typing import Any

from .connection import CIMV2_NAMESPACE, Connection, WmiFailure


def host_status(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    rows = connection.query(
        "SELECT Name, HypervisorPresent, NumberOfLogicalProcessors, TotalPhysicalMemory "
        "FROM Win32_ComputerSystem",
        namespace=CIMV2_NAMESPACE,
    )
    if len(rows) != 1:
        raise WmiFailure(f"Expected one Win32_ComputerSystem instance; found {len(rows)}.")
    computer = rows[0]
    services = [
        {"name": row.get("Name"), "state": row.get("State"), "start_mode": row.get("StartMode")}
        for row in connection.query(
            "SELECT Name, State, StartMode FROM Win32_Service "
            "WHERE Name = 'vmms' OR Name = 'vmcompute'",
            namespace=CIMV2_NAMESPACE,
        )
    ]
    return {
        "computer_name": computer.get("Name"),
        "hypervisor_present": bool(computer.get("HypervisorPresent")),
        "logical_processor_count": int(computer.get("NumberOfLogicalProcessors") or 0),
        "memory_capacity_bytes": int(computer.get("TotalPhysicalMemory") or 0),
        "services": services,
    }
