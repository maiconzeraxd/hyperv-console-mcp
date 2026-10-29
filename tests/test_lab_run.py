from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import BackendError, PolicyError, ValidationError
from hyperv_console_mcp.lab import run as lab_run
from hyperv_console_mcp.service import GATED_TOOLS, HyperVService

DIGEST = "a" * 64


class FakeLabService:
    """Just enough of HyperVService for the run/cycle orchestration."""

    def __init__(
        self,
        *,
        state: str = "Running",
        uptime: int = 500,
        after: dict[str, Any] | None = None,
        verification: bool = True,
        run_result: dict[str, Any] | None = None,
        run_error: Exception | None = None,
        events: list[dict[str, Any]] | None = None,
        evidence: bool = True,
    ) -> None:
        self.state = state
        self.uptime = uptime
        self.after = after
        self.verification = verification
        self.run_result = run_result
        self.run_error = run_error
        self.events = events
        self.evidence = evidence
        self.calls: list[tuple[str, Any]] = []
        self.commands: list[str] = []
        self.files: dict[str, bytes] = {}
        self.settings = Settings(evidence_root=Path("C:/Evidence"))

    # -- host side ---------------------------------------------------------
    def get_vm(self, vm_name: str) -> dict[str, Any]:
        self.calls.append(("vm_get", vm_name))
        if self.calls.count(("vm_get", vm_name)) > 1 and self.after is not None:
            return {"vm": self.after}
        return {"vm": {"name": vm_name, "state": self.state, "uptime_seconds": self.uptime}}

    def create_checkpoint(self, vm_name: str, name: str) -> dict[str, Any]:
        self.calls.append(("checkpoint_create", name))
        return {"checkpoint": {"id": "11111111-1111-1111-1111-111111111111", "name": name}}

    def list_checkpoints(self, vm_name: str) -> dict[str, Any]:
        self.calls.append(("checkpoint_list", vm_name))
        return {"checkpoints": [
            {"id": "11111111-1111-1111-1111-111111111111", "name": "pre-run-1"},
        ]}

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

    # -- guest side --------------------------------------------------------
    def guest_exec(
        self, vm_name: str, command: str, *, timeout_seconds: int | None = None
    ) -> dict[str, Any]:
        self.commands.append(command)
        if "Test-DriverLabPackage" in command:
            self.calls.append(("verify", timeout_seconds))
            return {"execution": {"success": self.verification, "exit_code": 0, "output": ""}}
        if "New-DriverLabEvidence" in command:
            self.calls.append(("evidence", timeout_seconds))
            if not self.evidence:
                return {"execution": {"success": False, "exit_code": 1, "error": "no dump"}}
            return {"execution": {"success": True, "exit_code": 0, "output": "{}"}}
        self.calls.append(("run", timeout_seconds))
        if self.run_error is not None:
            raise self.run_error
        return {"execution": {"success": True, "exit_code": 0, "output": "HVMCP_JSON"}}

    def guest_read_file(self, vm_name: str, path: str, max_bytes: int = 65_536) -> dict[str, Any]:
        key = path.casefold()
        if key.endswith(r"runs\run-1\result.json") and self.run_result is not None:
            data = json.dumps(self.run_result).encode()
        elif key.endswith(r"evidence\run-1\evidence.json"):
            data = json.dumps({"schema": 1, "files": [{
                "path": "logs/run.log",
                "size": len(b"evidence"),
                "sha256": hashlib.sha256(b"evidence").hexdigest(),
            }]}).encode()
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


def test_cycle_keeps_the_report_when_the_guest_cannot_produce_evidence() -> None:
    service = FakeLabService(
        run_error=BackendError("the guest session dropped", code="bridge_transport"),
        after={"state": "Running", "uptime_seconds": 5},
        evidence=False,
    )
    report = lab_run.cycle(
        service, "NORTH-Test",
        package_id="north.1", run_id="run-1",
        expected_manifest_sha256=DIGEST,
    )

    assert report["outcome"] == "bugcheck"
    assert report["evidence"]["collected"] is False
    assert report["revert"] is None


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
