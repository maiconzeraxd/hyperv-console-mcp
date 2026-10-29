"""Autonomous driver-lab runs: checkpoint, execute, classify, collect, revert.

`lab_run` is deliberately synchronous. The guest worker blocks for the length of
the run, while the host worker keeps answering `vm_get` and console calls. When
the call returns or times out, the VM state and uptime decide whether the guest
bugchecked, hung, or simply ran out of time.
"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from ..errors import HyperVError, PolicyError, ValidationError
from .transfer import GUEST_ROOT, DriverLabTransfer, identifier

RUNNER = rf"{GUEST_ROOT}\Tools\Invoke-DriverLabRun.ps1"
MAX_RESULT_BYTES = 1024 * 1024
# A reboot moves the guest's uptime back to the time since it restarted, so a
# lower uptime than the pre-run baseline is the reliable bugcheck signal.
REBOOT_GRACE_SECONDS = 30
OUTCOMES = ("ok", "failed", "not_verified", "bugcheck", "hang", "timeout")
_HASH = re.compile(r"[0-9a-fA-F]{64}")


def _digest(value: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValidationError("expected_manifest_sha256 must be a SHA-256 hexadecimal string.")
    return value.lower()


def _timeout(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 10 <= value <= 3600:
        raise ValidationError("timeout_seconds must be an integer between 10 and 3600.")
    return value


def checkpoint_name(run_id: str) -> str:
    return f"pre-{run_id}"


def result_path(run_id: str) -> str:
    return rf"{GUEST_ROOT}\Runs\{run_id}\result.json"


def run_command(package_id: str, run_id: str, digest: str) -> str:
    """The exact guest command. Both identifiers are validated, so nothing can break out."""
    return (
        f"& '{RUNNER}' -PackageId '{package_id}' -RunId '{run_id}' "
        f"-ExpectedManifestSha256 '{digest}' "
        f"-ApproveExecution 'EXECUTE {package_id} {run_id}'"
    )


def _read_result(service: Any, vm_name: str, run_id: str) -> dict[str, Any] | None:
    try:
        fetched = service.guest_read_file(vm_name, result_path(run_id), max_bytes=MAX_RESULT_BYTES)
        raw = base64.b64decode(fetched["file"]["content_base64"], validate=True)
        record = json.loads(raw.decode("utf-8"))
    except (HyperVError, KeyError, ValueError, TypeError):
        # No result file is exactly what a bugcheck or a hang looks like.
        return None
    return record if isinstance(record, dict) else None


def _vm_state(service: Any, vm_name: str) -> dict[str, Any] | None:
    try:
        return service.get_vm(vm_name)["vm"]
    except HyperVError:
        return None


def _classify(
    *,
    execution: dict[str, Any] | None,
    record: dict[str, Any] | None,
    error: HyperVError | None,
    after: dict[str, Any] | None,
    baseline_uptime: int,
    elapsed: float,
    timeout: int,
) -> tuple[str, str]:
    if record is not None:
        if record.get("success"):
            return "ok", "the guest runner finished and reported success"
        code = record.get("exit_code")
        return "failed", f"the guest runner finished with exit code {code}"
    if execution is not None:
        # The guest answered, so the VM is alive; the runner simply did not
        # leave a result file behind.
        return "failed", "the guest answered but wrote no result file"
    if after is None:
        return "hang", "the guest is unreachable and the host could not read the VM state"
    state = str(after.get("state", ""))
    uptime = after.get("uptime_seconds")
    if isinstance(uptime, int) and not isinstance(uptime, bool):
        expected = baseline_uptime + int(elapsed)
        if uptime < baseline_uptime or uptime + REBOOT_GRACE_SECONDS < expected:
            return (
                "bugcheck",
                f"the guest stopped answering and the VM rebooted "
                f"(uptime {uptime}s against {expected}s before the run)",
            )
    if state.casefold() != "running":
        return "hang", f"the guest stopped answering and the VM is {state or 'in an unknown state'}"
    code = getattr(error, "code", "") if error is not None else ""
    if code == "timeout":
        return "timeout", f"the run did not finish within {timeout}s"
    return "hang", f"the guest stopped answering while the VM kept running ({code or 'no detail'})"


def _bugcheck_events(service: Any, vm_name: str) -> list[dict[str, Any]] | None:
    """Best effort: the guest may be gone, so a failure here is not an error."""
    try:
        result = service.guest_events(
            vm_name, log_name="System", max_events=5, event_id=1001, hours=2
        )
    except HyperVError:
        return None
    events = result.get("events")
    return events if isinstance(events, list) else None


def _report(
    vm_name: str,
    package_id: str,
    run_id: str,
    *,
    outcome: str,
    detail: str,
    started_utc: str | None,
    elapsed: float,
    timeout: int,
    checkpoint: dict[str, Any] | None,
    verification: dict[str, Any] | None,
    execution: dict[str, Any] | None,
    record: dict[str, Any] | None,
    after: dict[str, Any] | None,
    bugcheck: list[dict[str, Any]] | None,
    payload_executed: bool,
) -> dict[str, Any]:
    return {
        "vm": vm_name,
        "package_id": package_id,
        "run_id": run_id,
        "outcome": outcome,
        "detail": detail,
        "outcome_values": list(OUTCOMES),
        "started_utc": started_utc,
        "finished_utc": datetime.now(timezone.utc).isoformat() if started_utc else None,
        "duration_seconds": elapsed,
        "timeout_seconds": timeout,
        "checkpoint": checkpoint,
        "verification": verification,
        "execution": execution,
        "result": record,
        "vm_after": after,
        "bugcheck": bugcheck,
        "payload_executed": payload_executed,
    }


def run(
    service: Any,
    vm_name: str,
    *,
    package_id: str,
    run_id: str,
    expected_manifest_sha256: str,
    timeout_seconds: int = 600,
    checkpoint_before: bool = True,
) -> dict[str, Any]:
    package = identifier(package_id, "package_id")
    run_identifier = identifier(run_id, "run_id")
    digest = _digest(expected_manifest_sha256)
    timeout = _timeout(timeout_seconds)
    if not isinstance(checkpoint_before, bool):
        raise ValidationError("checkpoint_before must be a boolean value.")

    before = service.get_vm(vm_name)["vm"]
    state = str(before.get("state", ""))
    if state.casefold() != "running":
        raise PolicyError(
            f"lab_run needs the VM Running; it is {state or 'unknown'}. "
            "The run never changes power state on its own."
        )
    uptime = before.get("uptime_seconds")
    baseline_uptime = uptime if isinstance(uptime, int) and not isinstance(uptime, bool) else 0

    checkpoint: dict[str, Any] | None = None
    if checkpoint_before:
        created = service.create_checkpoint(vm_name, checkpoint_name(run_identifier))
        snapshot = created.get("checkpoint") or {}
        checkpoint = {
            "name": checkpoint_name(run_identifier),
            "id": snapshot.get("id"),
            "created": True,
        }

    verification = DriverLabTransfer(service).verify(vm_name, package, digest)
    if not verification.get("verified"):
        return _report(
            vm_name, package, run_identifier,
            outcome="not_verified",
            detail="the package did not match its manifest, so nothing was executed",
            started_utc=None, elapsed=0.0, timeout=timeout, checkpoint=checkpoint,
            verification=verification, execution=None, record=None, after=None,
            bugcheck=None, payload_executed=False,
        )

    started_utc = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    execution: dict[str, Any] | None = None
    error: HyperVError | None = None
    try:
        execution = service.guest_exec(
            vm_name, run_command(package, run_identifier, digest), timeout_seconds=timeout
        )["execution"]
    except HyperVError as exc:
        error = exc
    elapsed = round(time.monotonic() - started, 3)

    record = _read_result(service, vm_name, run_identifier) if execution is not None else None
    after = _vm_state(service, vm_name)
    outcome, detail = _classify(
        execution=execution, record=record, error=error, after=after,
        baseline_uptime=baseline_uptime, elapsed=elapsed, timeout=timeout,
    )
    bugcheck = _bugcheck_events(service, vm_name) if outcome == "bugcheck" else None
    return _report(
        vm_name, package, run_identifier,
        outcome=outcome, detail=detail, started_utc=started_utc, elapsed=elapsed,
        timeout=timeout, checkpoint=checkpoint, verification=verification,
        execution=execution, record=record, after=after, bugcheck=bugcheck,
        payload_executed=True,
    )


def _restore(service: Any, vm_name: str, run_id: str) -> dict[str, Any]:
    wanted = checkpoint_name(run_id)
    try:
        listed = service.list_checkpoints(vm_name).get("checkpoints", [])
    except HyperVError as exc:
        return {"restored": False, "error": exc.code, "message": str(exc)}
    match = next(
        (item for item in listed if str(item.get("name", "")).casefold() == wanted.casefold()),
        None,
    )
    if match is None:
        return {"restored": False, "reason": f"checkpoint {wanted} is not present on the VM"}
    snapshot_id = str(match.get("id", ""))
    try:
        result = service.restore_checkpoint(vm_name, snapshot_id, vm_name, snapshot_id)
    except HyperVError as exc:
        return {"restored": False, "error": exc.code, "message": str(exc)}
    return {
        "restored": bool(result.get("restored")),
        "checkpoint": wanted,
        "snapshot_id": snapshot_id,
    }


DEBUGGERS = ("none", "serial", "kdnet")


def _debugger_report(
    service: Any,
    vm_name: str,
    transport: str,
    attached: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    """After a run: let kd say what happened, then parse it when it was a bugcheck."""
    debug: dict[str, Any] = {
        "transport": transport,
        "attached": True,
        "log_path": attached.get("log_path"),
        "event": None,
        "analysis": None,
    }
    try:
        event = service.kd_wait_event(vm_name, timeout_ms=2_000)
    except HyperVError as exc:
        debug["error"] = exc.code
        return debug
    debug["event"] = event.get("event")
    debug["module"] = event.get("module")
    if event.get("event") != "bugcheck":
        return debug
    report["outcome"] = "bugcheck"
    report["detail"] = (
        "kd stopped the target on a bugcheck; the parsed analysis is in debugger.analysis"
    )
    try:
        debug["analysis"] = service.kd_analyze(vm_name).get("report")
    except HyperVError as exc:
        debug["analysis_error"] = exc.code
    return debug


def cycle(
    service: Any,
    vm_name: str,
    *,
    package_id: str,
    run_id: str,
    expected_manifest_sha256: str,
    timeout_seconds: int = 600,
    checkpoint_before: bool = True,
    revert_after: bool = False,
    include_kernel_dump: bool = True,
    debugger: str = "none",
) -> dict[str, Any]:
    """One autonomous iteration: run, collect evidence, optionally revert.

    With a debugger attached, a bugcheck no longer looks like a hang: kd stops
    at the prompt and the cycle reports the bugcheck plus the parsed analysis
    instead of guessing from uptime.
    """
    if not isinstance(revert_after, bool):
        raise ValidationError("revert_after must be a boolean value.")
    if not isinstance(include_kernel_dump, bool):
        raise ValidationError("include_kernel_dump must be a boolean value.")
    if debugger not in DEBUGGERS:
        raise ValidationError(f"debugger must be one of: {', '.join(DEBUGGERS)}.")
    attached: dict[str, Any] | None = None
    if debugger != "none":
        attached = service.kd_attach(vm_name, debugger)
    report = run(
        service, vm_name,
        package_id=package_id, run_id=run_id,
        expected_manifest_sha256=expected_manifest_sha256,
        timeout_seconds=timeout_seconds, checkpoint_before=checkpoint_before,
    )
    if attached is not None:
        report["debugger"] = _debugger_report(service, vm_name, debugger, attached, report)
    evidence: dict[str, Any]
    if not report["payload_executed"]:
        evidence = {"collected": False, "reason": "the run did not execute a payload"}
    else:
        try:
            collected = DriverLabTransfer(service).collect(
                vm_name, report["package_id"], report["run_id"],
                service.settings.evidence_root.expanduser().resolve(),
                include_kernel_dump=include_kernel_dump,
            )
            evidence = {"collected": True, **collected}
        except HyperVError as exc:
            # A guest that bugchecked may never produce evidence; the run report
            # still has to come back.
            evidence = {"collected": False, "error": exc.code, "message": str(exc)}
    report["evidence"] = evidence
    report["revert"] = (
        _restore(service, vm_name, report["run_id"]) if revert_after else None
    )
    return report
