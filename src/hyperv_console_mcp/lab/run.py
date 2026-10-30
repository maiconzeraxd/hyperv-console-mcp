"""Autonomous driver-lab runs: checkpoint, execute, classify, collect, revert.

`lab_run` is deliberately synchronous. The guest worker blocks for the length of
the run, while the host worker keeps answering `vm_get` and console calls. When
the call returns or times out, the VM state and uptime decide whether the guest
bugchecked, hung, or simply ran out of time.
"""

from __future__ import annotations

import base64
import json
import numbers
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ..errors import HyperVError, PolicyError, ValidationError
from .recording import MAX_ERROR_CHARS, Recorder
from .report import write as report_write
from .transfer import GUEST_ROOT, DriverLabTransfer, guest_script, identifier

RUNNER = rf"{GUEST_ROOT}\Tools\Invoke-DriverLabRun.ps1"
MAX_RESULT_BYTES = 1024 * 1024
# Wide enough to read a bugcheck on a 1080p desktop, small enough that sixty
# frames fit the GIF budget without thinning.
RECORD_MAX_WIDTH = 640
# A reboot moves the guest's uptime back to the time since it restarted, so a
# lower uptime than the pre-run baseline is the reliable bugcheck signal.
REBOOT_GRACE_SECONDS = 30
# A guest that bugchecked can spend several seconds writing its dump while the
# VM still reports Running and a high uptime. Classifying on the first sample
# called that a hang, so the state is re-read a few times before deciding.
SETTLE_POLL_SECONDS = 1.5
SETTLE_POLL_ATTEMPTS = 4
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
    return guest_script(
        RUNNER,
        f"-PackageId '{package_id}'",
        f"-RunId '{run_id}'",
        f"-ExpectedManifestSha256 '{digest}'",
        f"-ApproveExecution 'EXECUTE {package_id} {run_id}'",
    )


def _seconds(value: Any) -> float | None:
    """Read an uptime the bridge rounded to a float.

    `vm_get` reports `uptime_seconds` as `[Math]::Round($Vm.Uptime.TotalSeconds, 3)`,
    which always arrives as a float. An earlier `isinstance(value, int)` test
    therefore never matched, so the baseline stayed at 0 and every crash+reboot
    was classified as a hang. Any real number counts; bool is not a number here.
    """
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    return float(value)


def _read_result(service: Any, vm_name: str, run_id: str) -> dict[str, Any] | None:
    try:
        fetched = service.guest_read_file(vm_name, result_path(run_id), max_bytes=MAX_RESULT_BYTES)
        raw = base64.b64decode(fetched["file"]["content_base64"], validate=True)
        # `Set-Content -Encoding UTF8` on Windows PowerShell 5.1 writes a BOM, and
        # json.loads rejects one; the older files keep theirs, so read tolerantly.
        record = json.loads(raw.decode("utf-8-sig"))
    except (HyperVError, KeyError, ValueError, TypeError):
        # No result file is exactly what a bugcheck or a hang looks like.
        return None
    return record if isinstance(record, dict) else None


def _vm_state(service: Any, vm_name: str) -> dict[str, Any] | None:
    try:
        return service.get_vm(vm_name)["vm"]
    except HyperVError:
        return None


def _settle_state(
    service: Any, vm_name: str, baseline_uptime: float, after: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Give the VM a moment to finish what a crash started.

    While a dump is being written the guest is unreachable but the VM still
    reports Running with a high uptime, which looks exactly like a hang. Polling
    briefly catches the reboot (or the power-off) that follows.
    """
    for _ in range(SETTLE_POLL_ATTEMPTS - 1):
        if after is None:
            break
        uptime = _seconds(after.get("uptime_seconds"))
        rebooted = uptime is not None and uptime < baseline_uptime
        running = str(after.get("state", "")).casefold() == "running"
        if rebooted or not running:
            break
        time.sleep(SETTLE_POLL_SECONDS)
        after = _vm_state(service, vm_name)
    return after


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _crashes_since(
    events: list[dict[str, Any]] | None, started_utc: str | None
) -> list[dict[str, Any]]:
    """The host-recorded crashes that happened during this run, oldest first."""
    start = _parse_time(started_utc)
    if not events or start is None:
        return []
    found: list[tuple[datetime, dict[str, Any]]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        when = _parse_time(event.get("time"))
        if when is None or when < start:
            continue
        found.append((when, event))
    found.sort(key=lambda item: item[0])
    return [event for _, event in found]


def _classify(
    *,
    execution: dict[str, Any] | None,
    record: dict[str, Any] | None,
    error: HyperVError | None,
    after: dict[str, Any] | None,
    baseline_uptime: float,
    elapsed: float,
    timeout: int,
    crash: dict[str, Any] | None = None,
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
    if crash is not None:
        # The host saw the crash as it happened, which beats inferring one from
        # uptime after the fact.
        code = crash.get("bugcheck_code") or "an unknown code"
        return "bugcheck", f"the host recorded a guest crash during the run (bugcheck {code})"
    if after is None:
        return "hang", "the guest is unreachable and the host could not read the VM state"
    state = str(after.get("state", ""))
    uptime = _seconds(after.get("uptime_seconds"))
    if uptime is not None:
        expected = baseline_uptime + int(elapsed)
        if uptime < baseline_uptime or uptime + REBOOT_GRACE_SECONDS < expected:
            return (
                "bugcheck",
                f"the guest stopped answering and the VM rebooted "
                f"(uptime {uptime:g}s against {expected}s before the run)",
            )
    if state.casefold() != "running":
        return "hang", f"the guest stopped answering and the VM is {state or 'in an unknown state'}"
    code = getattr(error, "code", "") if error is not None else ""
    if code == "timeout":
        return "timeout", f"the run did not finish within {timeout}s"
    return "hang", f"the guest stopped answering while the VM kept running ({code or 'no detail'})"


def _host_crashes(service: Any, vm_name: str) -> list[dict[str, Any]] | None:
    """Hyper-V's crash record for this VM, read on the host."""
    try:
        result = service.host_crash_events(vm_name, hours=2, max_events=20)
    except HyperVError:
        return None
    events = result.get("events")
    return events if isinstance(events, list) else None


def _guest_bugcheck_events(service: Any, vm_name: str) -> list[dict[str, Any]] | None:
    """Best effort: the guest may be gone, so a failure here is not an error."""
    try:
        result = service.guest_events(
            vm_name, log_name="System", max_events=5, event_id=1001, hours=2
        )
    except HyperVError:
        return None
    events = result.get("events")
    return events if isinstance(events, list) else None


def _bugcheck_events(
    service: Any, vm_name: str, host_events: list[dict[str, Any]] | None,
    started_utc: str | None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The crash evidence to report, and where it came from.

    The host writes the crash the moment it happens; the guest's event 1001 only
    appears after it has rebooted and logged far enough to write it, so it is the
    fallback rather than the primary source.
    """
    recent = _crashes_since(host_events, started_utc)
    if recent:
        return recent, "host"
    guest_events = _guest_bugcheck_events(service, vm_name)
    if guest_events:
        return guest_events, "guest"
    return None, None


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
    bugcheck_source: str | None = None,
    manifest_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "vm": vm_name,
        "package_id": package_id,
        "run_id": run_id,
        # The digest the run was asked to match, kept even when the package did
        # not verify: it is what a later run is compared against.
        "manifest_sha256": manifest_sha256,
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
        "bugcheck_source": bugcheck_source,
        "payload_executed": payload_executed,
    }


def _say(progress: Any, percent: int, message: str) -> None:
    """Tell the live feed which step just began, when anybody is listening.

    The numbers are the share of the wall time each step usually takes, not a
    measurement of this run: the guest's execution is one blocking call and has
    no progress of its own to report.
    """
    if progress is not None:
        progress(percent, message)


def band(progress: Any, low: int, high: int) -> Any:
    """Map a nested step's 0-100 onto a slice of the outer step's scale."""
    if progress is None:
        return None
    span = high - low
    return lambda percent, message: progress(low + int(span * max(0, min(100, percent)) / 100), message)


def run(
    service: Any,
    vm_name: str,
    *,
    package_id: str,
    run_id: str,
    expected_manifest_sha256: str,
    timeout_seconds: int = 600,
    checkpoint_before: bool = True,
    progress: Any = None,
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
    uptime = _seconds(before.get("uptime_seconds"))
    baseline_uptime = uptime if uptime is not None else 0.0

    checkpoint: dict[str, Any] | None = None
    if checkpoint_before:
        _say(progress, 5, "creating the pre-run checkpoint")
        created = service.create_checkpoint(vm_name, checkpoint_name(run_identifier))
        snapshot = created.get("checkpoint") or {}
        checkpoint = {
            "name": checkpoint_name(run_identifier),
            "id": snapshot.get("id"),
            "created": True,
        }

    _say(progress, 15, "verifying the package against its manifest")
    verification = DriverLabTransfer(service).verify(vm_name, package, digest)
    if not verification.get("verified"):
        return _report(
            vm_name, package, run_identifier,
            outcome="not_verified",
            detail="the package did not match its manifest, so nothing was executed",
            started_utc=None, elapsed=0.0, timeout=timeout, checkpoint=checkpoint,
            verification=verification, execution=None, record=None, after=None,
            bugcheck=None, payload_executed=False, manifest_sha256=digest,
        )

    started_utc = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    _say(progress, 25, "executing the staged package in the guest")
    execution: dict[str, Any] | None = None
    error: HyperVError | None = None
    try:
        execution = service.guest_exec(
            vm_name, run_command(package, run_identifier, digest), timeout_seconds=timeout
        )["execution"]
    except HyperVError as exc:
        error = exc
    elapsed = round(time.monotonic() - started, 3)
    _say(progress, 90, "the guest runner returned; reading what it left")

    record = _read_result(service, vm_name, run_identifier) if execution is not None else None
    after = _vm_state(service, vm_name)
    host_events: list[dict[str, Any]] | None = None
    crash: dict[str, Any] | None = None
    if execution is None and record is None:
        # The guest stopped answering. Give the VM a moment to finish a dump and
        # reboot, then ask the host whether it recorded a crash: that is a
        # definite answer, where uptime arithmetic is only an inference.
        after = _settle_state(service, vm_name, baseline_uptime, after)
        host_events = _host_crashes(service, vm_name)
        crashes = _crashes_since(host_events, started_utc)
        crash = crashes[-1] if crashes else None
    outcome, detail = _classify(
        execution=execution, record=record, error=error, after=after,
        baseline_uptime=baseline_uptime, elapsed=elapsed, timeout=timeout, crash=crash,
    )
    bugcheck: list[dict[str, Any]] | None = None
    bugcheck_source: str | None = None
    if outcome == "bugcheck":
        bugcheck, bugcheck_source = _bugcheck_events(
            service, vm_name, host_events, started_utc
        )
    return _report(
        vm_name, package, run_identifier,
        outcome=outcome, detail=detail, started_utc=started_utc, elapsed=elapsed,
        timeout=timeout, checkpoint=checkpoint, verification=verification,
        execution=execution, record=record, after=after, bugcheck=bugcheck,
        payload_executed=True, bugcheck_source=bugcheck_source, manifest_sha256=digest,
    )


def _restore(
    service: Any, vm_name: str, run_id: str, snapshot_id: str | None = None
) -> dict[str, Any]:
    """Restore the checkpoint this cycle made.

    The id from `create_checkpoint` is used directly when it is known: a listing
    taken moments after the checkpoint was created can still miss it, and the id
    is the only thing that identifies *this* cycle's checkpoint anyway. The name
    lookup stays as the fallback for a caller that has no id.
    """
    wanted = checkpoint_name(run_id)
    if not snapshot_id:
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
    snapshot_id = str(snapshot_id)
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


def frame_reader(service: Any, vm_name: str) -> Any:
    """A callable the recorder samples: one PNG of the console, or nothing.

    The display size is asked for once and then reused, because it cannot change
    mid-run and the per-frame cost is what decides how many frames fit a run.
    """
    size: dict[str, Any] | None = None

    def read() -> bytes | None:
        nonlocal size
        if size is None:
            size = service.console_frame_size(vm_name)
        return service.console_frame(
            vm_name, size["width"], size["height"], RECORD_MAX_WIDTH
        )["png"]

    return read


def staging_directory(root: Path | str, run_id: str) -> Path:
    """Where a recording is written while its run is still going.

    ``DriverLabTransfer.collect`` creates the run's own directory and refuses one
    that already exists, so the GIF cannot be written there first. It is staged
    beside the run and moved in once the evidence is home.
    """
    return Path(root) / f".recording-{run_id}"


def file_recording(recording: dict[str, Any], root: Path | str, run_id: str) -> dict[str, Any]:
    """Move a finished recording into the run's evidence directory."""
    name = str(recording.get("path") or "")
    source = staging_directory(root, run_id) / name
    target = Path(root) / str(run_id) / name
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
    except OSError as exc:
        recording["written"] = False
        recording["error"] = str(exc)[:MAX_ERROR_CHARS]
    finally:
        shutil.rmtree(staging_directory(root, run_id), ignore_errors=True)
    return recording


def _driver_modules(service: Any, vm_name: str, package_id: str) -> list[str]:
    """The ``.sys`` modules a staged package ships, named the way kd expects.

    ``sxe ld:<module>`` needs the module name without the extension. The manifest
    is read from the guest because the cycle only knows the package id; a missing
    or unreadable manifest simply means no break-on-load is armed.
    """
    manifest_path = rf"{GUEST_ROOT}\Packages\{package_id}\lab-manifest.json"
    try:
        fetched = service.guest_read_file(vm_name, manifest_path, max_bytes=MAX_RESULT_BYTES)
        raw = base64.b64decode(fetched["file"]["content_base64"], validate=True)
        manifest = json.loads(raw.decode("utf-8"))
    except (HyperVError, KeyError, ValueError, TypeError):
        return []
    entries = manifest.get("files") if isinstance(manifest, dict) else None
    modules: list[str] = []
    for entry in entries if isinstance(entries, list) else []:
        path = str(entry.get("path") or "") if isinstance(entry, dict) else ""
        if path.casefold().endswith(".sys"):
            stem = PurePosixPath(path).stem
            if stem and stem not in modules:
                modules.append(stem)
    return modules


def _arm_driver_breakpoints(service: Any, vm_name: str, package_id: str) -> dict[str, Any]:
    """Arm ``sxe ld:<driver>`` for every driver the package ships, before it runs."""
    modules = _driver_modules(service, vm_name, package_id)
    armed: list[str] = []
    error: str | None = None
    for module in modules:
        try:
            service.kd_break_on_load(vm_name, module)
        except HyperVError as exc:
            error = exc.code
            break
        armed.append(module)
    return {"modules": modules, "armed": armed, "error": error}



def _driver_symbols(service: Any, vm_name: str, modules: list[str]) -> dict[str, Any]:
    """Did the test driver's symbols load? A stack of raw addresses answers less.

    The PDB is staged beside the package and every lab directory holding one is
    on ``_NT_SYMBOL_PATH``, but a driver built against another Windows build, or
    one whose PDB did not travel with it, still resolves to a module and no
    function names. kd is the only one that knows which of those happened.
    """
    entries: list[dict[str, Any]] = []
    for module in modules:
        try:
            entries.append(service.kd_symbols(vm_name, module))
        except HyperVError as exc:
            entries.append({"module": module, "loaded": None, "error": exc.code})
    return {
        "modules": entries,
        "resolved": [item["module"] for item in entries if item.get("loaded") is True],
        "unresolved": [item["module"] for item in entries if item.get("loaded") is False],
        "all_loaded": (
            all(item.get("loaded") is True for item in entries) if entries else None
        ),
    }


def _debugger_report(
    service: Any,
    vm_name: str,
    transport: str,
    attached: dict[str, Any],
    report: dict[str, Any],
    modules: list[str] | None = None,
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
    # Only a broken-in target can answer this, which is why it is asked here
    # rather than after every run.
    debug["symbols"] = _driver_symbols(service, vm_name, list(modules or []))
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
    record: bool = False,
    progress: Any = None,
) -> dict[str, Any]:
    """One autonomous iteration: run, collect evidence, optionally revert.

    With a debugger attached, a bugcheck no longer looks like a hang: kd stops
    at the prompt and the cycle reports the bugcheck plus the parsed analysis
    instead of guessing from uptime. With ``record``, the console is sampled
    while the package runs and written beside the evidence as one GIF.
    """
    if not isinstance(revert_after, bool):
        raise ValidationError("revert_after must be a boolean value.")
    if not isinstance(include_kernel_dump, bool):
        raise ValidationError("include_kernel_dump must be a boolean value.")
    if not isinstance(record, bool):
        raise ValidationError("record must be a boolean value.")
    if debugger not in DEBUGGERS:
        raise ValidationError(f"debugger must be one of: {', '.join(DEBUGGERS)}.")
    attached: dict[str, Any] | None = None
    breakpoints: dict[str, Any] | None = None
    if debugger != "none":
        _say(progress, 3, f"attaching kd over {debugger}")
        attached = service.kd_attach(vm_name, debugger)
        # Arm the break before the payload can load it, so the run stops at
        # DriverEntry instead of racing the first break-in.
        _say(progress, 8, "arming the driver-load breakpoint")
        breakpoints = _arm_driver_breakpoints(service, vm_name, package_id)
    recorder: Recorder | None = None
    root = service.settings.evidence_root.expanduser().resolve()
    if record:
        _say(progress, 9, "recording the console while the package runs")
        recorder = Recorder(frame_reader(service, vm_name))
        recorder.start()
    report = run(
        service, vm_name,
        package_id=package_id, run_id=run_id,
        expected_manifest_sha256=expected_manifest_sha256,
        timeout_seconds=timeout_seconds, checkpoint_before=checkpoint_before,
        progress=band(progress, 10, 60),
    )
    if recorder is not None:
        # Stopped before the evidence is collected, so the last frame is the
        # screen the run stopped on and the download is not in the picture.
        _say(progress, 63, "writing the console recording")
        report["recording"] = recorder.stop(staging_directory(root, report["run_id"]))
    if attached is not None:
        _say(progress, 62, "asking kd what the target did")
        debug = _debugger_report(
            service, vm_name, debugger, attached, report,
            (breakpoints or {}).get("modules") or [],
        )
        debug["break_on_load"] = breakpoints
        report["debugger"] = debug
    evidence: dict[str, Any]
    if not report["payload_executed"]:
        evidence = {"collected": False, "reason": "the run did not execute a payload"}
    else:
        _say(progress, 75, "collecting evidence from the guest")
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
    recording = report.get("recording")
    if isinstance(recording, dict):
        if recording.get("written"):
            file_recording(recording, root, report["run_id"])
        else:
            # Nothing was written, so the staging directory only has to go.
            shutil.rmtree(staging_directory(root, report["run_id"]), ignore_errors=True)
    if report["outcome"] == "bugcheck":
        # The dump is on the host by now, so kd can say what the crash was even
        # when the run had no debugger attached. A failure here never costs the
        # run its report: the host's code and parameters are already in it.
        _say(progress, 88, "triaging the crash")
        from .crash import triage
        report["crash"] = triage(service, vm_name, report)
    checkpoint_id = (report.get("checkpoint") or {}).get("id")
    if revert_after:
        _say(progress, 94, "restoring the pre-run checkpoint")
    report["revert"] = (
        _restore(service, vm_name, report["run_id"], checkpoint_id) if revert_after else None
    )
    # Written last, so the record holds the revert and the crash reading too.
    _say(progress, 98, "writing the receipt")
    report["report_file"] = report_write(report, service.settings.evidence_root)
    return report
