"""Waiting on a Hyper-V method's job, and reading its refusal codes.

Every ``Msvm_*`` method answers the same way: ``0`` when it finished inside the
call, ``4096`` with an ``Msvm_ConcreteJob`` path when it did not, and one of the
3276x codes when it refused. The job is what carries the real failure, so the
wait and the translation live here, and every host operation shares them.
"""

from __future__ import annotations

import time
from typing import Any

from .connection import Connection, WmiFailure

COMPLETED = 0
JOB_STARTED = 4096

# The provider's own codes, as its method documentation numbers them. The
# parameters carry no ValueMap, so this table is the only place they are written
# down — and they are not the CIM ones, which is why 32775 is "invalid state"
# and not, say, an offset from a generic enum.
RETURN_CODES = {
    32768: ("backend_error", "the provider failed"),
    32769: ("access_denied", "this account may not change the VM"),
    32770: ("backend_error", "the provider does not support this operation"),
    32771: ("backend_error", "the provider returned an unknown status"),
    32772: ("timeout", "the provider timed out"),
    32773: ("invalid_argument", "the provider rejected a parameter"),
    32774: ("backend_error", "the VM is in use"),
    32775: ("invalid_state", "the VM is not in a state that allows this operation"),
    32776: ("invalid_argument", "the provider rejected the data type"),
    32777: ("backend_error", "the VM is not available"),
    32778: ("backend_error", "the provider is out of memory"),
}

JOB_COMPLETED = 7
JOB_FAILURES = {8: "was terminated", 9: "was killed", 10: "raised an exception"}
JOB_TIMEOUT_SECONDS = 60.0
JOB_POLL_SECONDS = 0.25


def await_job(
    connection: Connection, response: dict[str, Any], *, timeout: float | None = None
) -> None:
    """Wait for the operation, whether it finished in the call or in a job."""
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
            raise WmiFailure(f"The operation {JOB_FAILURES[state]}: {detail}.")
        if time.monotonic() >= deadline:
            raise WmiFailure(
                "The operation did not finish in time; it may still be running.", code="timeout"
            )
        time.sleep(JOB_POLL_SECONDS)


def _failure(code: int) -> WmiFailure:
    known = RETURN_CODES.get(code)
    if known is None:
        return WmiFailure(f"The provider refused the operation with code {code}.")
    name, description = known
    return WmiFailure(f"The provider refused the operation with code {code}: {description}.", code=name)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
