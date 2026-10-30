"""The same package on several VMs, one after another, compared side by side.

A driver that passes on Windows 10 and bugchecks on Windows 11 is one finding
that two receipts cannot state: they are two files in two folders. This runs the
same staged package through the same cycle on each VM and reduces the answers to
one table, so the difference is a column rather than a memory.

The runs are sequential, not concurrent. Each cycle takes the guest worker for
its whole length and holds a checkpoint, a debugger attachment and a guest
console; running two at once would interleave their evidence and their events
for a speed-up nobody asked for. One VM failing - off, not allowlisted, out of
disk - is recorded as that VM's row and the matrix continues, because the other
rows are still the point.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ..errors import HyperVError, ValidationError
from .run import OUTCOMES, band, cycle

MAX_VMS = 4
# The columns the matrix compares, in the order a reader wants them. They are
# the fields that decide whether two VMs behaved the same way.
FIELDS = (
    "outcome",
    "duration_seconds",
    "bugcheck_code",
    "blamed_driver",
    "symbols_all_loaded",
    "recording",
)
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def normalise_vms(vm_names: Any) -> list[str]:
    """The VM list as given, checked for count, duplicates and empties.

    The allowlist is the caller's job - it knows which VMs exist and what level
    each one has - but a matrix of one VM is a typo, not a matrix, and two rows
    for the same VM would overwrite each other's run id.
    """
    if isinstance(vm_names, str) or not isinstance(vm_names, (list, tuple)):
        raise ValidationError("vm_names must be a list of VM names.")
    names: list[str] = []
    seen: set[str] = set()
    for value in vm_names:
        name = str(value or "").strip()
        if not name:
            raise ValidationError("vm_names cannot contain an empty name.")
        if name.casefold() in seen:
            raise ValidationError(f"vm_names lists {name!r} twice.")
        seen.add(name.casefold())
        names.append(name)
    if len(names) < 2:
        raise ValidationError(
            "A matrix compares at least two VMs; use lab_cycle for one."
        )
    if len(names) > MAX_VMS:
        raise ValidationError(f"A matrix runs at most {MAX_VMS} VMs at a time.")
    return names


def run_id_for(base: str, vm_name: str) -> str:
    """One run id per VM, still a plain directory name under the evidence root."""
    slug = _UNSAFE.sub("-", vm_name).strip("-.") or "vm"
    return f"{base}-{slug}"


def _row(vm_name: str, run_id: str) -> dict[str, Any]:
    return {
        "vm": vm_name,
        "run_id": run_id,
        "outcome": None,
        "detail": None,
        "duration_seconds": None,
        "bugcheck_code": None,
        "blamed_driver": None,
        "symbols_all_loaded": None,
        "recording": None,
        "receipt": None,
        "error": None,
    }


def _fill(row: dict[str, Any], report: dict[str, Any]) -> None:
    crash = report.get("crash") if isinstance(report.get("crash"), dict) else {}
    bugcheck = crash.get("bugcheck") if isinstance(crash.get("bugcheck"), dict) else {}
    debug = report.get("debugger") if isinstance(report.get("debugger"), dict) else {}
    symbols = debug.get("symbols") if isinstance(debug.get("symbols"), dict) else {}
    recording = report.get("recording") if isinstance(report.get("recording"), dict) else {}
    written = report.get("report_file") if isinstance(report.get("report_file"), dict) else {}
    row.update(
        outcome=report.get("outcome"),
        detail=report.get("detail"),
        duration_seconds=report.get("duration_seconds"),
        # A crash the host recorded and a crash kd parsed describe the same
        # event; the triage reading is the one that names a driver.
        bugcheck_code=bugcheck.get("bugcheck_code") or crash.get("bugcheck_code"),
        blamed_driver=bugcheck.get("blamed_driver") or crash.get("blamed_driver"),
        symbols_all_loaded=symbols.get("all_loaded"),
        recording=recording.get("path") if recording.get("written") else None,
        receipt=written.get("path") if written.get("written") else None,
    )


def compare(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Field by field, which columns differ. A matrix of identical rows is a result."""
    differs: list[str] = []
    table: list[dict[str, Any]] = []
    for field in FIELDS:
        values = {str(row["vm"]): row.get(field) for row in rows}
        unique = {repr(value) for value in values.values()}
        same = len(unique) <= 1
        if not same:
            differs.append(field)
        table.append({"field": field, "same": same, "values": values})
    outcomes = {str(row["vm"]): row.get("outcome") for row in rows}
    return {
        "fields": table,
        "differs": differs,
        "same": not differs,
        "outcomes": outcomes,
        # The single sentence the DoD asks for: did every VM agree?
        "same_outcome": len({repr(value) for value in outcomes.values()}) <= 1,
    }


def _slice(progress: Any, index: int, count: int, vm_name: str) -> Any:
    """This VM's share of the bar, labelled with the VM it belongs to."""
    inner = band(progress, index * 100 // count, (index + 1) * 100 // count)
    if inner is None:
        return None
    return lambda percent, message: inner(percent, f"{vm_name}: {message}")


def matrix(
    service: Any,
    vm_names: list[str],
    *,
    package_id: str,
    run_id: str,
    expected_manifest_sha256: str,
    timeout_seconds: int = 600,
    checkpoint_before: bool = True,
    revert_after: bool = False,
    include_kernel_dump: bool = True,
    debugger: str = "none",
    record: bool = False,
    progress: Any = None,
) -> dict[str, Any]:
    names = normalise_vms(vm_names)
    started_utc = datetime.now(timezone.utc).isoformat()
    started = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for index, name in enumerate(names):
        vm_run_id = run_id_for(run_id, name)
        row = _row(name, vm_run_id)
        rows.append(row)
        slice_progress = _slice(progress, index, len(names), name)
        try:
            report = cycle(
                service, name,
                package_id=package_id, run_id=vm_run_id,
                expected_manifest_sha256=expected_manifest_sha256,
                timeout_seconds=timeout_seconds, checkpoint_before=checkpoint_before,
                revert_after=revert_after, include_kernel_dump=include_kernel_dump,
                debugger=debugger, record=record, progress=slice_progress,
            )
        except HyperVError as exc:
            # One VM refusing the run - off, no level, no guest - is a row, not
            # the end of the matrix: the other VMs still answer the question.
            row["error"] = exc.code
            row["detail"] = str(exc)
            continue
        _fill(row, report)
    finished = datetime.now(timezone.utc)
    comparison = compare(rows)
    return {
        "package_id": package_id,
        "run_id": run_id,
        "started_utc": started_utc,
        "finished_utc": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 1),
        "vms": rows,
        "count": len(rows),
        "outcome_values": list(OUTCOMES),
        **comparison,
    }


__all__ = [
    "FIELDS", "MAX_VMS", "compare", "matrix", "normalise_vms", "run_id_for",
]
