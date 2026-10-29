"""Power operations that outlive the request that started them.

A graceful shutdown runs for as long as the guest takes to answer, and saving
state takes as long as the memory is written; neither fits inside one bridge
call, which is why ``vm_power`` hands them off and ``operation_status`` reports
on them afterwards.

The bridge hands them to a PowerShell job. Here the provider's own objects are
enough, and each action keeps the one it started: saving answers a job
(``Msvm_ConcreteJob``), while a shutdown answers no job at all and is followed
through the VM's ``EnabledState`` instead. Both are read lazily, when
``operation_status`` is asked — there is no watcher thread, so nothing keeps
running when the server is idle, and the answer is always read now rather than
remembered from a moment ago.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime
from typing import Any

from .connection import Connection, WmiFailure, wql_literal
from .jobs import JOB_COMPLETED, JOB_FAILURES
from .timefmt import local_iso
from .vm import VM_STATES

# The bridge keeps the last 50 operations and drops the oldest finished ones.
_MAX_OPERATIONS = 50
# A shutdown that has not reached the target by now is reported as failed
# rather than as still running, which is what a stuck guest would otherwise look
# like for ever.
DEADLINE_SECONDS = 1800.0

_REGISTRY: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def begin(
    action: str,
    vm_name: str,
    *,
    vm_id: str,
    target_state: int,
    job_path: str | None = None,
) -> dict[str, Any]:
    """Register an operation the caller has already started."""
    identifier = uuid.uuid4().hex
    entry = {
        "operation_id": identifier,
        "action": action,
        "vm": vm_name,
        "started_at": local_iso(datetime.now().astimezone()),
        "state": "running",
        "succeeded": None,
        "error": None,
        "vm_id": vm_id,
        "target_state": target_state,
        "job": job_path,
        "deadline": time.monotonic() + DEADLINE_SECONDS,
    }
    with _LOCK:
        _REGISTRY[identifier] = entry
        _trim()
    return _answer(entry)


def status(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    """The operation as it stands now, in the shape the bridge answers."""
    identifier = str(payload.get("operation_id") or "").lower()
    with _LOCK:
        entry = _REGISTRY.get(identifier)
        if entry is None:
            return {
                "operation_id": identifier,
                "state": "unknown",
                "action": None,
                "vm": None,
                "succeeded": None,
                "error": None,
                "started_at": None,
            }
        # Held through the settle on purpose: it is one small read, and it keeps
        # two concurrent status calls from finishing the same entry twice.
        _settle(connection, entry)
        return _answer(entry)


def forget_all() -> None:
    """Empty the registry; the tests use it, the server never needs to."""
    with _LOCK:
        _REGISTRY.clear()


def _settle(connection: Connection, entry: dict[str, Any]) -> None:
    if entry["state"] != "running":
        return
    if entry["job"]:
        _settle_job(connection, entry)
    else:
        _settle_state(connection, entry)


def _settle_job(connection: Connection, entry: dict[str, Any]) -> None:
    try:
        job = connection.get(str(entry["job"]))
    except WmiFailure as exc:
        # The provider drops a finished job, so a job that cannot be read is
        # reported as a failure instead of raising out of operation_status.
        _finish(entry, "failed", False, str(exc))
        return
    state = _int(job.get("JobState"))
    if state == JOB_COMPLETED:
        _finish(entry, "completed", True, None)
    elif state in JOB_FAILURES:
        detail = job.get("ErrorDescription") or "no detail"
        _finish(entry, "failed", False, f"The operation {JOB_FAILURES[state]}: {detail}.")


def _settle_state(connection: Connection, entry: dict[str, Any]) -> None:
    state = _vm_state(connection, str(entry["vm_id"]))
    if state == entry["target_state"]:
        _finish(entry, "completed", True, None)
    elif time.monotonic() >= entry["deadline"]:
        found = VM_STATES.get(state, state) if state is not None else "gone"
        _finish(
            entry,
            "failed",
            False,
            f"The VM was still {found} after {DEADLINE_SECONDS:.0f}s.",
        )


def _vm_state(connection: Connection, identifier: str) -> int | None:
    rows = connection.query(
        "SELECT EnabledState FROM Msvm_ComputerSystem "
        f"WHERE Name = {wql_literal(identifier)}"
    )
    if len(rows) != 1:
        return None
    return _int(rows[0].get("EnabledState"))


def _finish(entry: dict[str, Any], state: str, succeeded: bool, error: str | None) -> None:
    entry["state"] = state
    entry["succeeded"] = succeeded
    entry["error"] = error


def _answer(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": entry["operation_id"],
        "state": entry["state"],
        "action": entry["action"],
        "vm": entry["vm"],
        "succeeded": entry["succeeded"],
        "error": entry["error"],
        "started_at": entry["started_at"],
    }


def _trim() -> None:
    """Keep the registry bounded, dropping finished entries before live ones."""
    while len(_REGISTRY) > _MAX_OPERATIONS:
        finished = [
            identifier
            for identifier, entry in _REGISTRY.items()
            if entry["state"] != "running"
        ]
        del _REGISTRY[finished[0] if finished else next(iter(_REGISTRY))]


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
