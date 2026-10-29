"""Power transitions: one ``RequestStateChange``, then the job it starts.

Hyper-V answers either "done" or "a job is running"; ``jobs.await_job`` waits
for the second and turns the provider's own refusal codes into errors.
"""

from __future__ import annotations

from typing import Any

from .connection import Connection, WmiFailure
from .jobs import await_job
from .vm import describe, find_vm

# RequestedState numbers, as the provider defines them. The method takes the
# CIM values only: ``ValueMap`` on the parameter is 2, 3, 4, 6..11 and a vendor
# range. Resuming is therefore the same request as starting — a paused VM is
# enabled again — and 32777, the ``Resuming`` *EnabledState*, is refused with
# 32775 by the provider (confirmed live on this host).
REQUESTED_STATES = {
    "start": 2,  # Enabled
    "turn_off": 3,  # Disabled
    "pause": 9,  # Quiesce
    "reset": 11,  # Reset
    "resume": 2,  # Enabled
}

# These two need a job that outlives the call, which the PowerShell worker
# still owns until the operation registry moves into Python.
BACKGROUND = frozenset({"shutdown", "save"})


def vm_power(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "")
    if action in BACKGROUND:
        raise WmiFailure(
            f"{action} needs a background job, which the PowerShell worker still owns.",
            code="wmi_unsupported",
        )
    requested = REQUESTED_STATES.get(action)
    if requested is None:
        raise WmiFailure(f"Unsupported power action '{action}'.", code="invalid_argument")
    name = str(payload.get("vm") or "")
    target = find_vm(connection, name)
    response = connection.call(target["__PATH"], "RequestStateChange", RequestedState=requested)
    await_job(connection, response)
    return {
        "accepted": False,
        "operation_id": None,
        "action": action,
        "state": "completed",
        "started_at": None,
        "vm": describe(connection, find_vm(connection, name)),
    }
