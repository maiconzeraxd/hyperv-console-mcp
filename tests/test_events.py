from __future__ import annotations

import json

import pytest
from hyperv_console_mcp.errors import PolicyError
from hyperv_console_mcp.events import EventBus, EventSink
from hyperv_console_mcp.ipc import ApprovalBroker, request_approval


class FakeBackend:
    def __init__(self) -> None:
        self.calls = []

    def invoke(self, operation, payload, *, timeout=None):
        self.calls.append((operation, payload))
        return {"ok": True}


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
    assert len(list(tmp_path.glob("*.json"))) == 2


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


def test_approval_gate_runs_before_backend(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HYPERV_MCP_REQUIRE_DASHBOARD_APPROVAL", "true")
    monkeypatch.setenv("HYPERV_MCP_EVENT_DIRECTORY", str(tmp_path))
    backend = FakeBackend()
    sink = EventSink(backend, EventBus())
    broker = ApprovalBroker(lambda request: False)
    broker.start()
    try:
        with pytest.raises(PolicyError):
            sink.invoke("vm_power", {"vm": "NORTH-Test", "action": "reset"})
        assert backend.calls == []
        sink.invoke("vm_get", {"vm": "NORTH-Test"})
        assert backend.calls[-1][0] == "vm_get"
    finally:
        broker.close()
