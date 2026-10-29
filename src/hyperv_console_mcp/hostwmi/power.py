"""Power transitions: one ``RequestStateChange``, then the job it starts.

Hyper-V answers either "done" or "a job is running"; the job is what actually
carries the failure, so every non-trivial return code ends here as a
``WmiFailure`` with the provider's own number in the message.
"""

from __future__ import annotations

import time
from typing import Any

from .connection import Connection, WmiFailure
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

COMPLETED = 0
JOB_STARTED = 4096

# The provider's own codes, as Msvm_ComputerSystem.RequestStateChange documents
# them. The parameter's qualifiers carry no ValueMap, so this table is the only
# place the numbers are written down.
RETURN_CODES = {
    32768: ("backend_error", "the provider failed"),
    32769: ("access_denied", "this account may not change the VM"),
    32770: ("backend_error", "the provider does not support this transition"),
    32771: ("backend_error", "the provider returned an unknown status"),
    32772: ("timeout", "the provider timed out"),
    32773: ("invalid_argument", "the provider rejected a parameter"),
    32774: ("backend_error", "the VM is in use"),
    32775: ("invalid_state", "the VM is not in a state that allows this transition"),
    32776: ("invalid_argument", "the provider rejected the data type"),
    32777: ("backend_error", "the VM is not available"),
    32778: ("backend_error", "the provider is out of memory"),
}

JOB_COMPLETED = 7
JOB_FAILURES = {8: "was terminated", 9: "was killed", 10: "raised an exception"}
JOB_TIMEOUT_SECONDS = 60.0
JOB_POLL_SECONDS = 0.25


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


def await_job(connection: Connection, response: dict[str, Any], *, timeout: float | None = None) -> None:
    """Wait for the transition, whether it finished in the call or in a job."""
    code = _int(response.get("ReturnValue"))
    if code == COMPLETED:
        return
    if code != JOB_STARTED:
        raise _failure(code)
    path = str(response.get("Job") or "")
    if not path:
        raise WmiFailure("The provider reported a running job but named none.")
    deadline = time.monotonic() + (JOB_TIMEOUT_SECONDS if timeout is None else timeout)
    while True:
        job = connection.get(path)
        state = _int(job.get("JobState"))
        if state == JOB_COMPLETED:
            return
        if state in JOB_FAILURES:
            detail = job.get("ErrorDescription") or "no detail"
            raise WmiFailure(f"The transition {JOB_FAILURES[state]}: {detail}.")
        if time.monotonic() >= deadline:
            raise WmiFailure(
                "The transition did not finish in time; it may still be running.", code="timeout"
            )
        time.sleep(JOB_POLL_SECONDS)


def _failure(code: int) -> WmiFailure:
    known = RETURN_CODES.get(code)
    if known is None:
        return WmiFailure(f"The provider refused the transition with code {code}.")
    name, description = known
    return WmiFailure(f"The provider refused the transition with code {code}: {description}.", code=name)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
