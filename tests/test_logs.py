"""One structured log stream, one record shape, one offset.

The event bus records what an operation *was*; this records what the
subsystems *said* while it ran, so the window's dock can show the AI and the
operator working side by side. These tests pin the three properties a dock
depends on: a stable record shape, an offset that returns only what is new, and
a message that never carries a secret.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import ValidationError
from hyperv_console_mcp.events import EVENTS, EventSink
from hyperv_console_mcp.logs import LEVELS, SOURCES, LogBus, redact
from hyperv_console_mcp.service import HyperVService


def test_a_line_carries_the_fields_a_dock_filters_on(tmp_path: Path) -> None:
    bus = LogBus(tmp_path)

    record = bus.publish(
        "lab", "staged the package", vm="NORTH-Test", correlation_id="c1"
    )

    assert record.sequence == 1
    assert record.timestamp.endswith("+00:00")
    assert record.source == "lab"
    assert record.vm == "NORTH-Test"
    assert record.level == "info"
    assert record.correlation_id == "c1"
    assert record.message == "staged the package"


def test_a_reader_asks_from_an_offset_and_gets_only_what_is_new(tmp_path: Path) -> None:
    bus = LogBus(tmp_path)
    for index in range(3):
        bus.publish("service", f"line {index}")

    assert [record["message"] for record in bus.snapshot(2)["records"]] == ["line 2"]
    assert bus.snapshot(3)["records"] == []
    assert bus.snapshot(0)["latest_sequence"] == 3


def test_filters_narrow_by_vm_source_level_and_correlation(tmp_path: Path) -> None:
    bus = LogBus(tmp_path)
    bus.publish("lab", "one", vm="A", correlation_id="c1")
    bus.publish("kd", "two", vm="A", correlation_id="c2", level="warning")
    bus.publish("kd", "three", vm="B", correlation_id="c1", level="error")
    bus.publish("serial", "four", vm="B")

    def messages(**filters: Any) -> list[str]:
        return [record["message"] for record in bus.snapshot(0, **filters)["records"]]

    assert messages(vm="A") == ["one", "two"]
    assert messages(source="kd") == ["two", "three"]
    assert messages(correlation_id="c1") == ["one", "three"]
    # A level is a floor, not an equality: a dock showing warnings shows errors.
    assert messages(level="warning") == ["two", "three"]
    assert messages(vm="B", source="kd") == ["three"]


def test_an_unknown_source_or_level_is_refused(tmp_path: Path) -> None:
    bus = LogBus(tmp_path)

    with pytest.raises(ValidationError):
        bus.publish("nowhere", "message")
    with pytest.raises(ValidationError):
        bus.publish("service", "message", level="loud")
    assert bus.snapshot(0)["records"] == []
    assert "service" in SOURCES and "error" in LEVELS


def test_a_secret_in_a_message_is_masked(tmp_path: Path) -> None:
    bus = LogBus(tmp_path)

    bus.publish("worker", "connecting with password=Hunter2! and token: abc123")
    bus.publish("kd", "key=0f0e0d0c0b0a09080706050403020100")
    bus.publish("worker", "guest_exec completed in 120 ms")

    messages = [record["message"] for record in bus.snapshot(0)["records"]]
    joined = " ".join(messages)
    assert "Hunter2!" not in joined
    assert "abc123" not in joined
    assert "0f0e0d0c0b0a09080706050403020100" not in joined
    # An ordinary line survives the scrub untouched.
    assert messages[-1] == "guest_exec completed in 120 ms"
    assert redact("no secret here") == "no secret here"


def test_the_stream_is_written_as_daily_jsonl(tmp_path: Path) -> None:
    bus = LogBus(tmp_path)
    record = bus.publish("lab", "one line")

    files = sorted(tmp_path.glob("logs-*.jsonl"))
    assert len(files) == 1
    written = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert written == [
        {
            "sequence": record.sequence,
            "timestamp": record.timestamp,
            "source": "lab",
            "vm": None,
            "level": "info",
            "correlation_id": record.correlation_id,
            "message": "one line",
        }
    ]


def test_a_waiting_reader_does_not_hold_the_writer(tmp_path: Path) -> None:
    """The dock subscribes for up to 30 s; the service must keep working."""
    bus = LogBus(tmp_path)
    result: dict[str, Any] = {}

    def subscribe() -> None:
        result.update(bus.wait(0, timeout_seconds=10))

    reader = threading.Thread(target=subscribe)
    reader.start()
    time.sleep(0.1)
    started = time.monotonic()
    bus.publish("service", "while the reader waits")
    elapsed = time.monotonic() - started
    reader.join(timeout=5)

    assert not reader.is_alive(), "the wait returned when the line arrived"
    assert elapsed < 1.0, "publishing never queues behind a waiting reader"
    assert [record["message"] for record in result["records"]] == ["while the reader waits"]


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke(
        self, operation: str, payload: Mapping[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((operation, dict(payload)))
        if operation == "guest_exec":
            return {"vm": payload.get("vm"), "execution": {
                "success": True, "exit_code": 0, "output": "ok", "had_errors": False,
                "error_records": [], "output_truncated": False,
            }}
        return {"operation": operation}

    def close(self) -> None:
        return None


@pytest.fixture
def quiet_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite out of the operator's own event and log directories."""
    monkeypatch.setenv("HYPERV_MCP_EVENT_DIRECTORY", str(tmp_path / "events"))


def test_a_guest_exec_leaves_correlated_lines_without_the_command(quiet_bus: None) -> None:
    """Two lines per operation, one correlation id, and no command text."""
    from hyperv_console_mcp.logs import LOGS

    service = HyperVService(
        EventSink(FakeBackend(), EVENTS),
        Settings(
            allowed_vms=frozenset({"LOGS-Test"}),
            allow_guest=True,
            guest_username="Administrator",
            guest_password="secret",
        ),
    )
    before = LOGS.snapshot(0)["latest_sequence"]
    command = "Write-Output 'the-command-text'"

    service.guest_exec("LOGS-Test", command)

    records = LOGS.snapshot(before, vm="LOGS-Test")["records"]
    assert len(records) >= 2, "a start and a finish, not a single line"
    assert {record["source"] for record in records} == {"worker"}
    assert len({record["correlation_id"] for record in records}) == 1
    assert command not in json.dumps(records)
    # The same id the operation events carry, which is what groups the two streams.
    correlation = records[0]["correlation_id"]
    events = EVENTS.snapshot(0)["events"]
    assert correlation in {event["correlation_id"] for event in events}


def test_the_subscribe_door_validates_what_it_is_asked(quiet_bus: None) -> None:
    service = HyperVService(FakeBackend(), Settings())

    with pytest.raises(ValidationError):
        service.logs_subscribe(-1)
    with pytest.raises(ValidationError):
        service.logs_subscribe(0, level="loud")
    with pytest.raises(ValidationError):
        service.logs_subscribe(0, source="nowhere")
    answer = service.logs_subscribe(0, timeout_seconds=0)
    assert set(answer) == {"records", "latest_sequence"}

def test_redaction_is_a_pattern_matcher_and_the_tests_say_so() -> None:
    """The backstop, demonstrated — because the docs must not overclaim it.

    ``redact`` masks the shapes a secret is *written* in: ``password=…``,
    ``token: …``, a base64 blob. It cannot know that a bare word in a sentence
    is a secret, and this test keeps that limitation visible so nobody reads
    "the stream is redacted" as "the stream cannot carry a secret".
    """
    assert redact("password=hunter2") == "password=<redacted>"
    assert "hunter2" not in redact("senha: hunter2")
    # A loose value, in a sentence, with no key= around it: untouched.
    assert redact("the operator typed hunter2") == "the operator typed hunter2"


def test_a_failed_operation_logs_its_code_and_never_its_message(quiet_bus: None) -> None:
    """The boundary is the source: the message never enters the stream at all.

    The event carries ``error_code`` and a fixed sentence, and the log mirrors
    the code. So a message that happens to carry a secret — an exception built
    by a transport, a raw command's stderr — cannot reach the dock, whether or
    not the redaction regex would have caught it.
    """
    from hyperv_console_mcp.logs import LOGS

    class LeakyBackend(FakeBackend):
        """The transport fails with the secret in its own message."""

        def invoke(self, operation, payload, *, timeout=None):
            raise RuntimeError("auth failed for Administrator with hunter2 in the message")

    service = HyperVService(
        EventSink(LeakyBackend(), EVENTS),
        Settings(
            allowed_vms=frozenset({"LOGS-Test"}),
            allow_guest=True,
            guest_username="Administrator",
            guest_password="hunter2",
        ),
    )
    before = LOGS.snapshot(0)["latest_sequence"]

    with pytest.raises(Exception):
        service.guest_exec("LOGS-Test", "hostname")

    records = LOGS.snapshot(before)["records"]
    assert records, "the failure is on the stream"
    text = json.dumps(records)
    assert "hunter2" not in text
    # What is there is the code, which is what a reader can act on.
    assert any("failed" in record["message"] for record in records)
    assert any(record["level"] == "error" for record in records)

