from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import PolicyError
from hyperv_console_mcp.events import EventBus, EventSink
from hyperv_console_mcp.ipc import ApprovalBroker, _summary, request_approval
from hyperv_console_mcp.service import GATED_TOOLS, HyperVService

windows_only = pytest.mark.skipif(os.name != "nt", reason="named-pipe approval is Windows-only")
SOURCE = Path(__file__).resolve().parents[1] / "src" / "hyperv_console_mcp"


class FakeBackend:
    def __init__(self) -> None:
        self.calls = []

    def invoke(self, operation, payload, *, timeout=None):
        self.calls.append((operation, payload))
        # Mirror the bridge's guest_exec envelope so the transport can map it.
        if operation == "guest_exec":
            return {"vm": payload.get("vm"), "execution": {
                "success": True, "exit_code": 0, "output": "ok", "had_errors": False,
                "error_records": [], "output_truncated": False,
            }}
        return {"vm": payload.get("vm"), "ok": True}


class RecordingApproval:
    """Stands in for the named-pipe broker so approval logic runs on every platform."""

    def __init__(self, approved: bool = True) -> None:
        self.approved = approved
        self.calls: list[tuple[str, str | None, dict[str, Any] | None]] = []

    def __call__(self, operation, vm_name, summary=None) -> None:
        self.calls.append((operation, vm_name, summary))
        if not self.approved:
            raise PolicyError(f"The dashboard denied or did not approve {operation}.")


@pytest.fixture
def approvals(monkeypatch) -> RecordingApproval:
    monkeypatch.setenv("HYPERV_MCP_REQUIRE_DASHBOARD_APPROVAL", "true")
    recorder = RecordingApproval()
    monkeypatch.setattr("hyperv_console_mcp.ipc.request_approval", recorder)
    return recorder


@pytest.fixture
def service(monkeypatch, tmp_path) -> tuple[HyperVService, FakeBackend]:
    monkeypatch.setenv("HYPERV_MCP_EVENT_DIRECTORY", str(tmp_path))
    backend = FakeBackend()
    return HyperVService(backend, Settings(
        allowed_vms=frozenset({"NORTH-Test"}), allow_guest=True, allow_console=True,
        guest_username="Administrator", guest_password="secret",
    )), backend


def test_event_lifecycle_contains_metadata_not_exception_details(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HYPERV_MCP_EVENT_DIRECTORY", str(tmp_path))
    bus = EventBus()
    with bus.actor("mcp"):
        with pytest.raises(RuntimeError, match="sensitive details"):
            with bus.operation("guest_exec", "NORTH-Test"):
                raise RuntimeError("sensitive details")
    events = bus.snapshot()["events"]
    assert [event["phase"] for event in events] == ["started", "failed"]
    assert events[0]["correlation_id"] == events[1]["correlation_id"]
    assert events[0]["actor"] == "mcp"
    assert "sensitive details" not in json.dumps(events)
    lines = _log_lines(tmp_path)
    assert [json.loads(line)["phase"] for line in lines] == ["started", "failed"]


def _log_lines(directory: Path) -> list[str]:
    (path,) = sorted(directory.glob("events-*.jsonl"))
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_events_append_to_one_daily_jsonl_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HYPERV_MCP_EVENT_DIRECTORY", str(tmp_path))
    bus = EventBus()
    for index in range(3):
        with bus.operation(f"operation_{index}", "NORTH-Test"):
            pass
    assert len(list(tmp_path.glob("events-*.jsonl"))) == 1
    assert len(_log_lines(tmp_path)) == 6


def test_the_event_reader_resumes_from_its_offset(tmp_path, monkeypatch) -> None:
    from hyperv_console_mcp.events import EventLogReader

    monkeypatch.setenv("HYPERV_MCP_EVENT_DIRECTORY", str(tmp_path))
    bus = EventBus()
    reader = EventLogReader(tmp_path)
    with bus.operation("first", "NORTH-Test"):
        pass
    assert [event["phase"] for event in reader.read_new()] == ["started", "completed"]
    assert reader.read_new() == []
    with bus.operation("second", "NORTH-Test"):
        pass
    assert [event["operation"] for event in reader.read_new()] == ["second", "second"]


def test_old_event_logs_are_pruned_and_recent_ones_kept(tmp_path, monkeypatch) -> None:
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("HYPERV_MCP_EVENT_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("HYPERV_MCP_EVENT_RETENTION_DAYS", "2")
    stale = tmp_path / f"events-{(datetime.now(timezone.utc) - timedelta(days=9)):%Y%m%d}.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    fresh = tmp_path / f"events-{(datetime.now(timezone.utc) - timedelta(days=1)):%Y%m%d}.jsonl"
    fresh.write_text("{}\n", encoding="utf-8")
    with EventBus().operation("prune-check", "NORTH-Test"):
        pass
    assert not stale.exists()
    assert fresh.exists()


def test_nested_operations_share_only_the_outer_lifecycle(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HYPERV_MCP_EVENT_DIRECTORY", str(tmp_path))
    bus = EventBus()
    with bus.operation("vm_power", "NORTH-Test") as outer:
        assert outer
        with bus.operation("vm_get", "NORTH-Test") as nested:
            assert nested == ""
        assert bus.snapshot()["events"][0]["phase"] == "started"
    events = bus.snapshot()["events"]
    assert [(event["operation"], event["phase"]) for event in events] == [
        ("vm_power", "started"), ("vm_power", "completed")
    ]
    with bus.operation("vm_get") as following:
        assert following


def test_bridge_operations_are_not_approved_one_by_one(tmp_path, monkeypatch, approvals) -> None:
    monkeypatch.setenv("HYPERV_MCP_EVENT_DIRECTORY", str(tmp_path))
    backend = FakeBackend()
    sink = EventSink(backend, EventBus())
    # A tool-level approval covers every bridge call it makes; the sink must not
    # ask again for each file or chunk.
    for _ in range(5):
        sink.invoke("guest_write_file", {"vm": "NORTH-Test", "path": "C:\\x"})
    assert approvals.calls == []
    assert len(backend.calls) == 5


def test_one_tool_call_requests_exactly_one_approval(service, approvals) -> None:
    hyperv, backend = service
    result = hyperv.console_batch("NORTH-Test", [
        {"type": "keys", "keys": ["enter"]},
        {"type": "wait", "milliseconds": 1},
        {"type": "scroll", "delta": 1},
    ])
    assert result["actions_completed"] == 3
    assert [call[0] for call in approvals.calls] == ["console_batch"]
    assert approvals.calls[0][2] == {"actions": ["keys", "wait", "scroll"], "action_count": 3}
    assert [operation for operation, _ in backend.calls] == [
        "console_press_keys", "console_scroll"
    ]


def test_a_denied_approval_stops_the_tool_before_the_guest(service, approvals) -> None:
    hyperv, backend = service
    approvals.approved = False
    with pytest.raises(PolicyError, match="denied"):
        hyperv.guest_exec("NORTH-Test", "Get-Process")
    assert backend.calls == []


def test_guest_exec_approval_carries_an_informed_command_summary(service, approvals) -> None:
    hyperv, _ = service
    command = "\n".join(f"Write-Output {index}" for index in range(40))
    hyperv.guest_exec("NORTH-Test", command)
    _, _, summary = approvals.calls[0]
    assert summary["command_lines"] == 40
    assert summary["command_truncated"] is True
    assert summary["command_preview"].splitlines() == [
        f"Write-Output {index}" for index in range(20)
    ]
    assert len(summary["command_sha256"]) == 64
    assert "secret" not in json.dumps(summary)


def test_read_only_guest_tools_never_request_approval(service, approvals) -> None:
    hyperv, backend = service
    hyperv.guest_status("NORTH-Test")
    assert approvals.calls == []
    assert backend.calls[-1][0] == "guest_status"


def test_the_approval_summary_drops_secret_keys_and_stays_bounded() -> None:
    cleaned = _summary({
        "path": "C:\\DriverLab\\x.sys", "password": "hunter2", "guest_password": "hunter2",
        "kdnet_key": "1.2.3.4:50000", "nested": {"token": "abc", "size": 4},
    })
    assert cleaned == {"path": "C:\\DriverLab\\x.sys", "nested": {"size": 4}}
    assert _summary({"blob": ["x" * 4_000] * 8}) == {
        "truncated": True, "note": "The approval summary was too large to display.",
    }
    assert _summary("not a mapping") == {}


def test_every_mutating_mcp_tool_is_gated_and_read_only_lab_tools_are_not() -> None:
    import ast

    tree = ast.parse((SOURCE / "server.py").read_text(encoding="utf-8"))
    mutating = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "tool" or not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        annotations = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "annotations"), None
        )
        if isinstance(annotations, ast.Name) and annotations.id == "MUTATION":
            mutating.add(node.args[0].value)
    assert mutating, "no mutating tools were found"
    assert mutating == set(GATED_TOOLS)
    assert {"lab_verify", "lab_diagnostics"}.isdisjoint(GATED_TOOLS)

    # `_approve` gates directly; `_guest_call` gates when it is given a summary,
    # which is how the guest file tools reach the transport.
    gated = set()
    for node in ast.walk(ast.parse((SOURCE / "service.py").read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if node.func.attr == "_approve":
            gated.add(node.args[0].value)
        elif node.func.attr == "_guest_call" and any(
            keyword.arg == "summary" for keyword in node.keywords
        ):
            gated.add(node.args[0].value)
    assert set(GATED_TOOLS) <= gated


@windows_only
@pytest.mark.parametrize("approved", [True, False])
def test_real_named_pipe_approval(approved: bool) -> None:
    broker = ApprovalBroker(lambda request: approved)
    broker.start()
    try:
        if approved:
            request_approval("checkpoint_create", "NORTH-Test")
        else:
            with pytest.raises(PolicyError, match="denied"):
                request_approval("checkpoint_create", "NORTH-Test")
    finally:
        broker.close()


@windows_only
def test_the_broker_serves_many_sequential_and_concurrent_approvals() -> None:
    import threading

    seen: list[str] = []
    lock = threading.Lock()

    def callback(request: dict) -> bool:
        with lock:
            seen.append(request["request_id"])
        return True

    broker = ApprovalBroker(callback)
    broker.start()
    try:
        for _ in range(20):
            request_approval("guest_exec", "NORTH-Test", {"command_preview": "Get-Process"})

        errors: list[BaseException] = []

        def approve() -> None:
            try:
                request_approval("guest_exec", "NORTH-Test")
            except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread
                errors.append(exc)

        threads = [threading.Thread(target=approve) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    finally:
        broker.close()
    assert errors == []
    assert len(seen) == 24
    assert len(set(seen)) == 24


@windows_only
def test_a_second_broker_refuses_to_share_the_pipe() -> None:
    first = ApprovalBroker(lambda request: True)
    first.start()
    try:
        with pytest.raises(PolicyError, match="already owns the pipe"):
            ApprovalBroker(lambda request: True).start()
    finally:
        first.close()
