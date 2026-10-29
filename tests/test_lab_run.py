from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import BackendError, PolicyError, ValidationError
from hyperv_console_mcp.lab import run as lab_run
from hyperv_console_mcp.lab.transfer import canonical_json
from hyperv_console_mcp.service import GATED_TOOLS, HyperVService

# The manifest the fake guest reports, and the digest of its bytes: verification
# compares those bytes with the approved digest, exactly as the live path does.
MANIFEST = {
    "schema": 1,
    "package_id": "north.1",
    "files": [{"path": "Run-Lab.ps1", "size": 6, "sha256": hashlib.sha256(b"# noop").hexdigest()}],
}
MANIFEST_BYTES = canonical_json(MANIFEST)
DIGEST = hashlib.sha256(MANIFEST_BYTES).hexdigest()
EVIDENCE_FAILURE = (
    "New-DriverLabEvidence.ps1 : Cannot bind argument to parameter 'IncludeKernelDump'."
)


@pytest.fixture(autouse=True)
def _no_settle_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """The classifier waits for a reboot to show up; tests do not need the clock."""
    monkeypatch.setattr(lab_run, "SETTLE_POLL_SECONDS", 0)


class FakeLabService:
    """Just enough of HyperVService for the run/cycle orchestration."""

    def __init__(
        self,
        *,
        state: str = "Running",
        uptime: float = 500,
        after: dict[str, Any] | None = None,
        after_sequence: list[dict[str, Any]] | None = None,
        verification: bool = True,
        run_result: dict[str, Any] | None = None,
        run_error: Exception | None = None,
        events: list[dict[str, Any]] | None = None,
        host_events: list[dict[str, Any]] | None = None,
        evidence: bool = True,
        kd_event: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
        checkpoints: list[dict[str, Any]] | None = None,
        dumps: list[dict[str, Any]] | None = None,
        dump_report: dict[str, Any] | None = None,
        dump_error: Exception | None = None,
    ) -> None:
        self.state = state
        self.uptime = uptime
        self.after = after
        self.after_sequence = after_sequence
        self.verification = verification
        self.run_result = run_result
        self.run_error = run_error
        self.events = events
        self.host_events = host_events
        self.evidence = evidence
        self.kd_event = kd_event or {"event": "break", "module": None}
        self.manifest = manifest
        self.checkpoints = checkpoints
        self.dumps = dumps or []
        self.dump_report = dump_report
        self.dump_error = dump_error
        self.calls: list[tuple[str, Any]] = []
        self.commands: list[str] = []
        self.files: dict[str, bytes] = {}
        self.settings = Settings(evidence_root=Path("C:/Evidence"))

    # -- host side ---------------------------------------------------------
    def get_vm(self, vm_name: str) -> dict[str, Any]:
        self.calls.append(("vm_get", vm_name))
        seen = self.calls.count(("vm_get", vm_name))
        if seen > 1 and self.after is not None:
            if self.after_sequence is not None:
                # Successive reads model a VM that is still finishing a dump.
                index = min(seen - 2, len(self.after_sequence) - 1)
                return {"vm": self.after_sequence[index]}
            return {"vm": self.after}
        return {"vm": {"name": vm_name, "state": self.state, "uptime_seconds": self.uptime}}

    def create_checkpoint(self, vm_name: str, name: str) -> dict[str, Any]:
        self.calls.append(("checkpoint_create", name))
        return {"checkpoint": {"id": "11111111-1111-1111-1111-111111111111", "name": name}}

    def list_checkpoints(self, vm_name: str) -> dict[str, Any]:
        self.calls.append(("checkpoint_list", vm_name))
        if self.checkpoints is None:
            return {"checkpoints": [
                {"id": "11111111-1111-1111-1111-111111111111", "name": "pre-run-1"},
            ]}
        return {"checkpoints": self.checkpoints}

    def restore_checkpoint(
        self, vm_name: str, snapshot_id: str, confirm_vm: str, confirm_snapshot_id: str
    ) -> dict[str, Any]:
        self.calls.append(("checkpoint_restore", snapshot_id))
        return {"restored": True, "checkpoint": {"id": snapshot_id}}

    def guest_events(self, vm_name: str, **_: Any) -> dict[str, Any]:
        self.calls.append(("guest_events", vm_name))
        if self.events is None:
            raise BackendError("the guest is gone", code="bridge_transport")
        return {"events": self.events}

    def host_crash_events(
        self, vm_name: str, hours: int = 2, max_events: int = 20
    ) -> dict[str, Any]:
        self.calls.append(("host_crash_events", vm_name))
        if self.host_events is None:
            raise BackendError("the host log is not readable", code="access_denied")
        events = []
        for event in self.host_events:
            stamped = dict(event)
            # An event without a time is one the host writes right now, which is
            # what a crash during the run looks like.
            stamped.setdefault("time", datetime.now(timezone.utc).isoformat())
            events.append(stamped)
        return {"events": events}

    # -- guest side --------------------------------------------------------
    def guest_package_list(self, vm_name: str, package_id: str) -> dict[str, Any]:
        self.calls.append(("verify", None))
        files = [
            {"path": entry["path"], "size": entry["size"], "sha256": entry["sha256"]}
            for entry in MANIFEST["files"]
        ]
        if not self.verification:
            # Same listing, wrong hash: the package no longer matches its manifest.
            files = [{**entry, "sha256": "0" * 64} for entry in files]
        return {"package": {
            "package_id": package_id, "present": True, "manifest_present": True,
            "manifest_base64": base64.b64encode(MANIFEST_BYTES).decode("ascii"),
            "files": files,
        }}

    def guest_exec(
        self, vm_name: str, command: str, *, timeout_seconds: int | None = None
    ) -> dict[str, Any]:
        self.commands.append(command)
        if "New-DriverLabEvidence" in command:
            self.calls.append(("evidence", timeout_seconds))
            if not self.evidence:
                # A `-File` script reports a terminating error through its output,
                # with an exit code and no error records.
                return {"execution": {
                    "success": False, "exit_code": 1, "had_errors": False,
                    "error_records": [], "output": EVIDENCE_FAILURE,
                }}
            return {"execution": {"success": True, "exit_code": 0, "output": "{}"}}
        self.calls.append(("run", timeout_seconds))
        if self.run_error is not None:
            raise self.run_error
        return {"execution": {"success": True, "exit_code": 0, "output": "HVMCP_JSON"}}

    def guest_read_file(self, vm_name: str, path: str, max_bytes: int = 65_536) -> dict[str, Any]:
        key = path.casefold()
        if key.endswith(r"packages\north.1\lab-manifest.json") and self.manifest is not None:
            data = json.dumps(self.manifest).encode()
        elif key.endswith(r"runs\run-1\result.json") and self.run_result is not None:
            data = json.dumps(self.run_result).encode()
        elif key.endswith(r"evidence\run-1\evidence.json"):
            files = [{
                "path": "logs/run.log",
                "size": len(b"evidence"),
                "sha256": hashlib.sha256(b"evidence").hexdigest(),
            }, *self.dumps]
            data = json.dumps({"schema": 1, "files": files}).encode()
        else:
            raise BackendError(f"{path} not found", code="not_found")
        if len(data) > max_bytes:
            raise ValidationError("file is too large")
        return {"file": {"content_base64": base64.b64encode(data).decode()}}

    def guest_read_chunk(self, vm_name: str, path: str, offset: int, count: int) -> dict[str, Any]:
        data = b"evidence"[offset:offset + count]
        return {"chunk": {
            "content_base64": base64.b64encode(data).decode(),
            "sha256": hashlib.sha256(data).hexdigest(),
        }}

    # -- debugger side -----------------------------------------------------
    def kd_attach(self, vm_name: str, transport: str = "kdnet", **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("kd_attach", transport))
        return {"attached": True, "transport": transport, "log_path": "C:/kd/session.log"}

    def kd_wait_event(self, vm_name: str, timeout_ms: int | None = None) -> dict[str, Any]:
        self.calls.append(("kd_wait_event", timeout_ms))
        return dict(self.kd_event)

    def kd_analyze(self, vm_name: str) -> dict[str, Any]:
        self.calls.append(("kd_analyze", vm_name))
        return {"report": {"found": True, "bugcheck_code": "0xd1", "blamed_driver": "MyDriver.sys"}}

    def kd_dump_open(
        self, vm_name: str, dump_path: str, commands: list[str] | None = None
    ) -> dict[str, Any]:
        self.calls.append(("kd_dump_open", dump_path))
        if self.dump_error is not None:
            raise self.dump_error
        return {
            "dump": dump_path,
            "output": "KD OUTPUT",
            "duration_ms": 1_200,
            "report": self.dump_report or {
                "found": True, "bugcheck_code": "0x1e", "failure_bucket_id": "AV_MyDriver",
                "blamed_driver": "MyDriver.sys", "top_frames": [{"symbol": "MyDriver!Crash"}],
            },
        }

    def kd_break_on_load(self, vm_name: str, module: str, enabled: bool = True) -> dict[str, Any]:
        self.calls.append(("kd_break_on_load", module))
        return {"break_on_load": module, "enabled": enabled}


def _run(service: FakeLabService, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "package_id": "north.1",
        "run_id": "run-1",
        "expected_manifest_sha256": DIGEST,
    }
    arguments.update(overrides)
    return lab_run.run(service, "NORTH-Test", **arguments)


def test_run_checkpoints_then_verifies_then_executes() -> None:
    service = FakeLabService(run_result={"success": True, "exit_code": 0})
    report = _run(service)

    assert report["outcome"] == "ok"
    assert report["payload_executed"] is True
    assert report["checkpoint"] == {
        "name": "pre-run-1",
        "id": "11111111-1111-1111-1111-111111111111",
        "created": True,
    }
    order = [name for name, _ in service.calls]
    assert order[:4] == ["vm_get", "checkpoint_create", "verify", "run"]
    # The approval phrase is generated here and names exactly this package and run.
    run_command = service.commands[-1]
    assert "-ApproveExecution 'EXECUTE north.1 run-1'" in run_command
    assert "secret" not in run_command


def test_run_never_executes_a_package_that_fails_verification() -> None:
    service = FakeLabService(verification=False)
    report = _run(service)

    assert report["outcome"] == "not_verified"
    assert report["payload_executed"] is False
    assert [name for name, _ in service.calls] == ["vm_get", "checkpoint_create", "verify"]


def test_run_classifies_a_reboot_as_a_bugcheck() -> None:
    service = FakeLabService(
        run_error=BackendError("the guest session dropped", code="bridge_transport"),
        after={"state": "Running", "uptime_seconds": 20},
        events=[{"id": 1001, "message": "The computer has rebooted from a bugcheck."}],
    )
    report = _run(service)

    assert report["outcome"] == "bugcheck"
    assert "rebooted" in report["detail"]
    assert report["bugcheck"] == [{"id": 1001, "message": "The computer has rebooted from a bugcheck."}]
    assert report["payload_executed"] is True


def test_run_classifies_a_reboot_as_a_bugcheck_when_uptime_is_a_float() -> None:
    """Regression: the bridge rounds uptime, so it always arrives as a float.

    `vm_get` reports `[Math]::Round($Vm.Uptime.TotalSeconds, 3)`. The baseline and
    the classification both tested `isinstance(uptime, int)`, which never matched:
    the baseline stayed at 0 and a crash+reboot came back as a hang.
    """
    service = FakeLabService(
        uptime=500.0,
        run_error=BackendError("the guest session dropped", code="bridge_transport"),
        after={"state": "Running", "uptime_seconds": 20.5},
    )
    report = _run(service)

    assert report["outcome"] == "bugcheck"
    assert "20.5" in report["detail"]


def test_run_classifies_a_host_recorded_crash_as_a_bugcheck() -> None:
    """A guest writing its dump still looks like a running VM with a high uptime.

    Hyper-V writes event 18590 the moment the guest crashes, with the bugcheck
    code as a numeric field. That record decides the outcome; the uptime does not
    have to move first.
    """
    service = FakeLabService(
        run_error=BackendError("the guest session dropped", code="bridge_transport"),
        after={"state": "Running", "uptime_seconds": 900.0},
        host_events=[{
            "id": 18590,
            "vm_name": "NORTH-Test",
            "bugcheck_code": "0xd1",
            "parameters": ["0x1", "0x2", "0x3", "0x4"],
        }],
    )
    report = _run(service)

    assert report["outcome"] == "bugcheck"
    assert "0xd1" in report["detail"]
    assert report["bugcheck_source"] == "host"
    assert report["bugcheck"][0]["bugcheck_code"] == "0xd1"


def test_a_crash_from_before_the_run_does_not_classify_it() -> None:
    """The host log is a time window, so an older crash must not be claimed."""
    service = FakeLabService(
        run_error=BackendError("the guest session dropped", code="bridge_transport"),
        after={"state": "Running", "uptime_seconds": 500.0},
        host_events=[{
            "id": 18590,
            "time": "2020-01-01T00:00:00+00:00",
            "vm_name": "NORTH-Test",
            "bugcheck_code": "0x9f",
        }],
    )
    report = _run(service)

    assert report["outcome"] == "hang"
    assert report["bugcheck"] is None


def test_the_classifier_waits_for_a_reboot_that_arrives_late() -> None:
    """The reboot lands a moment after the guest call fails.

    Reading the state once called that a hang. The classifier polls briefly, so
    the reboot that follows the dump is still seen.
    """
    service = FakeLabService(
        uptime=500.0,
        run_error=BackendError("the guest session dropped", code="bridge_transport"),
        after={"state": "Running", "uptime_seconds": 900.0},
        after_sequence=[
            {"state": "Running", "uptime_seconds": 900.0},
            {"state": "Running", "uptime_seconds": 12.0},
        ],
    )
    report = _run(service)

    assert report["outcome"] == "bugcheck"
    assert service.calls.count(("vm_get", "NORTH-Test")) >= 3


def test_run_classifies_an_unreachable_but_running_guest_as_a_hang() -> None:
    service = FakeLabService(
        run_error=BackendError("the guest session dropped", code="bridge_transport"),
        after={"state": "Running", "uptime_seconds": 500},
    )
    report = _run(service)

    assert report["outcome"] == "hang"
    assert report["bugcheck"] is None


def test_run_classifies_an_expired_deadline_as_a_timeout() -> None:
    service = FakeLabService(
        run_error=BackendError("The local bridge timed out.", code="timeout"),
        after={"state": "Running", "uptime_seconds": 500},
    )
    report = _run(service, timeout_seconds=30)

    assert report["outcome"] == "timeout"
    assert report["timeout_seconds"] == 30
    # The guest command was given the run's own deadline, not the worker default.
    assert ("run", 30) in service.calls


def test_run_reports_a_payload_that_exited_non_zero() -> None:
    service = FakeLabService(run_result={"success": False, "exit_code": 3})
    report = _run(service)

    assert report["outcome"] == "failed"
    assert "3" in report["detail"]


def test_run_refuses_a_vm_that_is_not_running() -> None:
    service = FakeLabService(state="Off")
    with pytest.raises(PolicyError, match="Running"):
        _run(service)
    assert [name for name, _ in service.calls] == ["vm_get"]


def test_run_validates_its_arguments_before_touching_the_vm() -> None:
    service = FakeLabService()
    with pytest.raises(ValidationError, match="expected_manifest_sha256"):
        _run(service, expected_manifest_sha256="nope")
    with pytest.raises(ValidationError, match="timeout_seconds"):
        _run(service, timeout_seconds=5)
    with pytest.raises(ValidationError, match="run_id"):
        _run(service, run_id="../escape")
    assert service.calls == []


def test_cycle_collects_evidence_and_restores_the_checkpoint(tmp_path: Path) -> None:
    service = FakeLabService(run_result={"success": True, "exit_code": 0})
    service.settings = replace(service.settings, evidence_root=tmp_path)
    report = lab_run.cycle(
        service, "NORTH-Test",
        package_id="north.1", run_id="run-1",
        expected_manifest_sha256=DIGEST, revert_after=True,
    )

    assert report["outcome"] == "ok"
    assert report["evidence"]["collected"] is True
    assert (tmp_path / "run-1" / "logs" / "run.log").read_bytes() == b"evidence"
    assert report["revert"] == {
        "restored": True,
        "checkpoint": "pre-run-1",
        "snapshot_id": "11111111-1111-1111-1111-111111111111",
    }
    assert ("checkpoint_restore", "11111111-1111-1111-1111-111111111111") in service.calls


def test_cycle_keeps_the_report_when_the_guest_cannot_produce_evidence(
    tmp_path: Path,
) -> None:
    service = FakeLabService(
        run_error=BackendError("the guest session dropped", code="bridge_transport"),
        after={"state": "Running", "uptime_seconds": 5},
        evidence=False,
    )
    service.settings = replace(service.settings, evidence_root=tmp_path)
    report = lab_run.cycle(
        service, "NORTH-Test",
        package_id="north.1", run_id="run-1",
        expected_manifest_sha256=DIGEST,
    )

    assert report["outcome"] == "bugcheck"
    assert report["evidence"]["collected"] is False
    assert report["revert"] is None


def test_a_failed_evidence_collection_says_what_the_guest_said(tmp_path: Path) -> None:
    """A `-File` script reports through its output, so `error` is never set.

    Regression: the live cycle reported "Guest evidence collection failed:
    unknown error" while the guest had printed exactly what was wrong.
    """
    service = FakeLabService(
        run_result={"success": True, "exit_code": 0}, evidence=False,
    )
    service.settings = replace(service.settings, evidence_root=tmp_path)
    report = lab_run.cycle(
        service, "NORTH-Test",
        package_id="north.1", run_id="run-1",
        expected_manifest_sha256=DIGEST,
    )

    assert report["evidence"]["collected"] is False
    assert "Cannot bind argument" in report["evidence"]["message"]


def test_evidence_collection_omits_the_switch_instead_of_passing_a_string(
    tmp_path: Path,
) -> None:
    """`-File` hands a script its arguments as text.

    Regression: `-IncludeKernelDump:$false` reached the guest as the string
    "$false", which cannot bind to a SwitchParameter, so evidence never
    collected on a live VM.
    """
    service = FakeLabService(run_result={"success": True, "exit_code": 0})
    service.settings = replace(service.settings, evidence_root=tmp_path / "off")
    lab_run.cycle(
        service, "NORTH-Test",
        package_id="north.1", run_id="run-1",
        expected_manifest_sha256=DIGEST, include_kernel_dump=False,
    )
    command = next(item for item in service.commands if "New-DriverLabEvidence" in item)
    assert "-IncludeKernelDump" not in command
    assert "$false" not in command and "$true" not in command

    with_dump = FakeLabService(run_result={"success": True, "exit_code": 0})
    with_dump.settings = replace(with_dump.settings, evidence_root=tmp_path / "on")
    lab_run.cycle(
        with_dump, "NORTH-Test",
        package_id="north.1", run_id="run-1",
        expected_manifest_sha256=DIGEST, include_kernel_dump=True,
    )
    command = next(item for item in with_dump.commands if "New-DriverLabEvidence" in item)
    assert command.endswith("-IncludeKernelDump")


def test_run_reads_a_result_file_that_carries_a_utf8_bom() -> None:
    """Windows PowerShell's `Set-Content -Encoding UTF8` writes a BOM.

    Regression: the live runner wrote a perfectly good result.json and the host
    still reported "the guest answered but wrote no result file", because
    `json.loads` rejects a leading BOM.
    """
    service = FakeLabService(run_result={"success": True, "exit_code": 0})
    original = service.guest_read_file

    def with_bom(vm_name: str, path: str, max_bytes: int = 65_536) -> dict[str, Any]:
        result = original(vm_name, path, max_bytes)
        if path.casefold().endswith("result.json"):
            data = base64.b64decode(result["file"]["content_base64"])
            result["file"]["content_base64"] = base64.b64encode(
                b"\xef\xbb\xbf" + data
            ).decode()
        return result

    service.guest_read_file = with_bom  # type: ignore[method-assign]
    report = _run(service)

    assert report["outcome"] == "ok"
    assert report["result"]["success"] is True


def test_cycle_does_not_collect_after_a_package_failed_verification(tmp_path: Path) -> None:
    service = FakeLabService(verification=False)
    service.settings = replace(service.settings, evidence_root=tmp_path)
    report = lab_run.cycle(
        service, "NORTH-Test",
        package_id="north.1", run_id="run-1",
        expected_manifest_sha256=DIGEST,
    )

    assert report["outcome"] == "not_verified"
    assert report["evidence"]["collected"] is False
    assert "evidence" not in [name for name, _ in service.calls]


def test_cycle_reverts_by_the_id_it_created_not_by_a_fresh_listing(
    tmp_path: Path,
) -> None:
    """A listing taken seconds after `Checkpoint-VM` can still miss the new snapshot.

    Regression: on a live VM the checkpoint was created and then reported as
    "not present on the VM", so `revert_after=true` silently did nothing.
    """
    service = FakeLabService(
        run_result={"success": True, "exit_code": 0},
        checkpoints=[],
    )
    service.settings = replace(service.settings, evidence_root=tmp_path)
    report = lab_run.cycle(
        service, "NORTH-Test",
        package_id="north.1", run_id="run-1",
        expected_manifest_sha256=DIGEST, revert_after=True,
    )

    assert report["revert"] == {
        "restored": True,
        "checkpoint": "pre-run-1",
        "snapshot_id": "11111111-1111-1111-1111-111111111111",
    }
    assert "checkpoint_list" not in [name for name, _ in service.calls]


# -- debugger integration ---------------------------------------------------


def test_cycle_attaches_kd_and_reports_a_bugcheck_instead_of_a_hang(tmp_path: Path) -> None:
    service = FakeLabService(
        run_error=BackendError("the guest session dropped", code="bridge_transport"),
        after={"state": "Running", "uptime_seconds": 900},
        evidence=False,
        kd_event={"event": "bugcheck", "module": None},
    )
    service.settings = replace(service.settings, evidence_root=tmp_path)
    report = lab_run.cycle(
        service, "NORTH-Test",
        package_id="north.1", run_id="run-1",
        expected_manifest_sha256=DIGEST, debugger="kdnet",
    )

    assert ("kd_attach", "kdnet") in service.calls
    assert ("kd_wait_event", 2_000) in service.calls
    assert report["outcome"] == "bugcheck"
    assert report["debugger"]["event"] == "bugcheck"
    assert report["debugger"]["analysis"]["bugcheck_code"] == "0xd1"
    assert ("kd_analyze", "NORTH-Test") in service.calls


def test_cycle_arms_a_break_on_load_for_each_driver_before_the_run(tmp_path: Path) -> None:
    service = FakeLabService(
        run_result={"success": True, "exit_code": 0},
        manifest={"schema": 1, "package_id": "north.1", "files": [
            {"path": "MyDriver.sys"},
            {"path": "sub/Helper.sys"},
            {"path": "notes.txt"},
        ]},
    )
    service.settings = replace(service.settings, evidence_root=tmp_path)
    report = lab_run.cycle(
        service, "NORTH-Test",
        package_id="north.1", run_id="run-1",
        expected_manifest_sha256=DIGEST, debugger="kdnet",
    )

    assert report["debugger"]["break_on_load"] == {
        "modules": ["MyDriver", "Helper"],
        "armed": ["MyDriver", "Helper"],
        "error": None,
    }
    order = [name for name, _ in service.calls]
    # The break has to be armed before the payload can load it.
    assert order.index("kd_break_on_load") < order.index("run")
    assert [value for name, value in service.calls if name == "kd_break_on_load"] == [
        "MyDriver", "Helper",
    ]


def test_cycle_arms_nothing_when_the_package_ships_no_driver(tmp_path: Path) -> None:
    service = FakeLabService(
        run_result={"success": True, "exit_code": 0},
        manifest={"schema": 1, "package_id": "north.1", "files": [{"path": "Run-Lab.ps1"}]},
    )
    service.settings = replace(service.settings, evidence_root=tmp_path)
    report = lab_run.cycle(
        service, "NORTH-Test",
        package_id="north.1", run_id="run-1",
        expected_manifest_sha256=DIGEST, debugger="serial",
    )

    assert report["debugger"]["break_on_load"] == {"modules": [], "armed": [], "error": None}
    assert not [name for name, _ in service.calls if name == "kd_break_on_load"]


def test_cycle_without_a_debugger_never_touches_kd(tmp_path: Path) -> None:
    service = FakeLabService(run_result={"success": True, "exit_code": 0})
    service.settings = replace(service.settings, evidence_root=tmp_path)
    report = lab_run.cycle(
        service, "NORTH-Test",
        package_id="north.1", run_id="run-1",
        expected_manifest_sha256=DIGEST,
    )
    assert "debugger" not in report
    assert not [name for name, _ in service.calls if name.startswith("kd_")]


def _dump_entry(name: str, when: str) -> dict[str, Any]:
    return {
        "path": f"minidumps/{name}", "size": len(b"evidence"),
        "sha256": hashlib.sha256(b"evidence").hexdigest(), "modified_utc": when,
    }


def test_a_crash_without_a_debugger_still_gets_kds_reading(tmp_path: Path) -> None:
    """The dump is on the host after collection, so !analyze -v can run there.

    Without this, a bugcheck with no debugger attached said only that the guest
    had crashed; FAILURE_BUCKET_ID and the blamed module needed a manual kd run.
    """
    service = FakeLabService(
        run_error=BackendError("the guest session dropped", code="bridge_transport"),
        after={"state": "Running", "uptime_seconds": 5},
        host_events=[{"bugcheck_code": "0x1e", "parameters": ["0x1", "0x2", "0x3", "0x4"]}],
        dumps=[_dump_entry("old.dmp", "2026-10-29T09:00:00Z"),
               _dump_entry("new.dmp", "2026-10-29T12:00:00Z")],
    )
    service.settings = replace(service.settings, evidence_root=tmp_path)
    report = lab_run.cycle(
        service, "NORTH-Test",
        package_id="north.1", run_id="run-1",
        expected_manifest_sha256=DIGEST,
    )

    assert report["outcome"] == "bugcheck"
    crash = report["crash"]
    assert crash["analyzed"] is True
    assert crash["dump"].endswith("new.dmp")
    assert crash["bugcheck"]["failure_bucket_id"] == "AV_MyDriver"
    assert crash["bugcheck"]["blamed_driver"] == "MyDriver.sys"
    assert crash["bugcheck"]["bugcheck_parameters"] == ["0x1", "0x2", "0x3", "0x4"]
    assert (tmp_path / "run-1" / "kd-analyze.log").read_text(encoding="utf-8") == "KD OUTPUT"


def test_a_crash_with_no_dump_says_so_and_keeps_the_hosts_record(tmp_path: Path) -> None:
    service = FakeLabService(
        run_error=BackendError("the guest session dropped", code="bridge_transport"),
        after={"state": "Running", "uptime_seconds": 5},
        host_events=[{"bugcheck_code": "0x9f", "parameters": ["0x3"]}],
    )
    service.settings = replace(service.settings, evidence_root=tmp_path)
    report = lab_run.cycle(
        service, "NORTH-Test",
        package_id="north.1", run_id="run-1",
        expected_manifest_sha256=DIGEST,
    )

    assert report["outcome"] == "bugcheck"
    assert report["crash"]["analyzed"] is False
    assert report["crash"]["reason"]
    assert report["bugcheck"][0]["bugcheck_code"] == "0x9f"
    assert not [name for name, _ in service.calls if name == "kd_dump_open"]


def test_a_run_that_did_not_crash_is_never_triaged(tmp_path: Path) -> None:
    service = FakeLabService(
        run_result={"success": True, "exit_code": 0},
        dumps=[_dump_entry("new.dmp", "2026-10-29T12:00:00Z")],
    )
    service.settings = replace(service.settings, evidence_root=tmp_path)
    report = lab_run.cycle(
        service, "NORTH-Test",
        package_id="north.1", run_id="run-1",
        expected_manifest_sha256=DIGEST,
    )

    assert report["outcome"] == "ok"
    assert "crash" not in report
    assert not [name for name, _ in service.calls if name == "kd_dump_open"]


def test_cycle_rejects_an_unknown_debugger_before_attaching() -> None:
    service = FakeLabService()
    with pytest.raises(ValidationError):
        lab_run.cycle(
            service, "NORTH-Test",
            package_id="north.1", run_id="run-1",
            expected_manifest_sha256=DIGEST, debugger="windbgx",
        )
    assert service.calls == []


# -- service boundary -------------------------------------------------------


def _service(**overrides: Any) -> tuple[HyperVService, Any]:
    class Backend:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def invoke(self, operation: str, payload: dict[str, Any], *, timeout=None):
            self.calls.append((operation, dict(payload)))
            return {"operation": operation}

    backend = Backend()
    settings = Settings(
        allowed_vms=frozenset({"NORTH-Test"}), allow_guest=True, allow_debugger=True,
        guest_username="Administrator", guest_password="secret",
    )
    return HyperVService(backend, replace(settings, **overrides)), backend


def test_lab_run_is_refused_outside_the_lab_profile(monkeypatch) -> None:
    service, backend = _service()
    monkeypatch.setattr(
        "hyperv_console_mcp.lab.run.run",
        lambda *args, **kwargs: pytest.fail("the lab run must not start"),
    )
    with pytest.raises(PolicyError, match="HYPERV_MCP_PROFILE=lab"):
        service.lab_run("NORTH-Test", "north.1", "run-1", DIGEST)
    with pytest.raises(PolicyError, match="HYPERV_MCP_PROFILE=lab"):
        service.lab_cycle("NORTH-Test", "north.1", "run-1", DIGEST)
    assert backend.calls == []


def test_lab_run_runs_in_the_lab_profile(monkeypatch) -> None:
    service, backend = _service(profile="lab")
    seen: dict[str, Any] = {}

    def fake(service_arg, vm_name, **kwargs):
        seen["vm"] = vm_name
        seen.update(kwargs)
        return {"outcome": "ok"}

    monkeypatch.setattr("hyperv_console_mcp.lab.run.run", fake)
    assert service.lab_run("NORTH-Test", "north.1", "run-1", DIGEST)["outcome"] == "ok"
    assert seen["vm"] == "NORTH-Test"
    assert seen["package_id"] == "north.1"
    assert seen["timeout_seconds"] == 600
    assert backend.calls == []


def test_lab_run_and_cycle_are_gated() -> None:
    assert {"lab_run", "lab_cycle", "vm_inject_nmi"} <= GATED_TOOLS
    assert "lab_verify" not in GATED_TOOLS


def test_nmi_needs_the_debugger_capability_and_the_exact_vm_name() -> None:
    service, backend = _service(allow_debugger=False)
    with pytest.raises(PolicyError, match="HYPERV_MCP_ALLOW_DEBUGGER"):
        service.vm_inject_nmi("NORTH-Test", "NORTH-Test")
    assert backend.calls == []

    service, backend = _service()
    with pytest.raises(PolicyError, match="confirm_vm"):
        service.vm_inject_nmi("NORTH-Test", "north-test")
    assert backend.calls == []

    result = service.vm_inject_nmi("NORTH-Test", "NORTH-Test")
    assert result["operation"] == "vm_inject_nmi"
    assert backend.calls[-1] == ("vm_inject_nmi", {"vm": "NORTH-Test"})


def test_the_run_command_bypasses_the_guest_execution_policy() -> None:
    """A stock guest is `Restricted`; the runner has to be started explicitly.

    Regression: `& 'C:\\\\DriverLab\\\\Tools\\\\Invoke-DriverLabRun.ps1' ...` was refused
    with "execution of scripts is disabled on this system" on a live VM.
    """
    command = lab_run.run_command("north.1", "run-1", DIGEST)
    assert command.startswith(
        "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File '"
    )
    assert "Invoke-DriverLabRun.ps1" in command
    assert "-ApproveExecution 'EXECUTE north.1 run-1'" in command
    assert f"-ExpectedManifestSha256 '{DIGEST}'" in command
