"""Power transitions: one ``RequestStateChange``, then the job it starts.

Hyper-V answers either "done" or "a job is running"; ``jobs.await_job`` waits
for the second and turns the provider's own refusal codes into errors.

A graceful shutdown and a save are the two that can outlive the call, so they
are started here and reported by ``operations`` afterwards.
"""

from __future__ import annotations

from typing import Any

from . import operations
from .connection import Connection, WmiFailure, wql_literal
from .jobs import JOB_STARTED, await_job, failure
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

# Saving state is RequestedState 6 — the CIM "Offline" value, which Hyper-V maps
# to Saved; the VM's EnabledState afterwards is 6 as well. The value the method
# documentation calls "Saving" (32773) is refused with 32775 on this host, and
# so is every other vendor value tried, including fast save (32779).
SAVING = 6

_OFF = 3
_SAVED = 6
_RUNNING = 2
SHUTDOWN_REASON = "HyperVConsoleMCP: a graceful shutdown was requested by the operator."
_WORDING = {"shutdown": "shut down gracefully", "save": "saved"}

# These two are started and then reported through operation_status, because a
# guest may take minutes to shut down and saving state writes all of memory.
BACKGROUND = frozenset({"shutdown", "save"})


def vm_power(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "")
    if action in BACKGROUND:
        return _background(connection, payload, action)
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


def _background(
    connection: Connection, payload: dict[str, Any], action: str
) -> dict[str, Any]:
    """Start a shutdown or a save, and answer before it finishes.

    The VM is described first, as the bridge does: once the transition is under
    way the same read would catch it half-way.
    """
    target = find_vm(connection, str(payload.get("vm") or ""))
    if _int(target.get("EnabledState")) != _RUNNING:
        raise WmiFailure(
            f"The VM must be Running before it can be {_WORDING[action]}. "
            "The server will not start it.",
            code="invalid_state",
        )
    snapshot = describe(connection, target)
    job = _start(connection, target, action)
    operation = operations.begin(
        action,
        str(target.get("ElementName") or ""),
        vm_id=str(target.get("Name") or ""),
        target_state=_OFF if action == "shutdown" else _SAVED,
        job_path=job,
    )
    return {
        "accepted": True,
        "operation_id": operation["operation_id"],
        "action": action,
        "state": operation["state"],
        "started_at": operation["started_at"],
        "vm": snapshot,
    }


def _start(connection: Connection, target: dict[str, Any], action: str) -> str | None:
    """Ask for the transition, and answer the job that carries it, if any.

    A graceful shutdown starts no job — ``InitiateShutdown`` answers only a
    return code, and the guest is the one doing the work — so it answers None.
    """
    if action == "shutdown":
        path = _shutdown_component(connection, str(target.get("Name") or ""))
        response = connection.call(
            path, "InitiateShutdown", Force=False, Reason=SHUTDOWN_REASON
        )
        code = _int(response.get("ReturnValue"))
        if code != 0:
            raise failure(code)
        return None
    response = connection.call(
        str(target.get("__PATH") or ""), "RequestStateChange", RequestedState=SAVING
    )
    code = _int(response.get("ReturnValue"))
    if code == 0:
        return None
    if code != JOB_STARTED:
        raise failure(code)
    job = str(response.get("Job") or "")
    if not job:
        raise WmiFailure("The provider reported a running job but named none.")
    return job


def _shutdown_component(connection: Connection, identifier: str) -> str:
    """The VM's shutdown service, the only way to ask for a graceful stop.

    ``RequestedState`` 4 (Shut Down) is in the ValueMap, but its documentation
    says it is valid in Hyper-V V1 only, so the component is the V2 path. Its
    ``Name`` is localized — "Desligamento" on this host — which is why it is
    found by ``SystemName``, the VM's own id.
    """
    rows = connection.query(
        f"SELECT * FROM Msvm_ShutdownComponent WHERE SystemName = {wql_literal(identifier)}"
    )
    if len(rows) != 1:
        raise WmiFailure(
            f"Expected one shutdown component for '{identifier}'; found {len(rows)}.",
            code="not_found",
        )
    return str(rows[0]["__PATH"])


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
