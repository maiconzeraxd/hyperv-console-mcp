from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from hyperv_console_mcp.audit import AuditLogger
from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import BackendError, PolicyError, ValidationError
from hyperv_console_mcp.service import HyperVService


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failure: Exception | None = None
        self.responses: dict[str, dict[str, Any]] = {}

    def invoke(
        self, operation: str, payload: Mapping[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((operation, dict(payload)))
        if self.failure:
            raise self.failure
        if operation == "vm_list":
            return {"vms": [{"name": "NORTH-Test"}, {"name": "Other"}]}
        return self.responses.get(operation, {"operation": operation})


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def enabled(backend: FakeBackend) -> HyperVService:
    return HyperVService(
        backend,
        Settings(
            allowed_vms=frozenset({"NORTH-Test"}),
            allow_power=True,
            allow_checkpoints=True,
            allow_guest=True,
            allow_console=True,
            allow_screenshots=True,
            allow_debugger=True,
            allow_media=True,
            guest_username="Administrator",
            guest_password="secret",
        ),
    )


def test_default_is_read_only(backend: FakeBackend) -> None:
    service = HyperVService(backend)
    with pytest.raises(PolicyError):
        service.power("NORTH-Test", "start")
    with pytest.raises(PolicyError):
        service.guest_exec("NORTH-Test", "hostname")
    with pytest.raises(PolicyError):
        service.console_type_text("NORTH-Test", "hello")
    with pytest.raises(PolicyError):
        service.authorize_screenshot("NORTH-Test")
    assert backend.calls == []


def test_read_list_is_filtered(enabled: HyperVService, backend: FakeBackend) -> None:
    result = enabled.list_vms()
    assert result["vms"] == [{"name": "NORTH-Test"}]
    assert result["allowlist_active"] is True
    enabled.get_vm("NORTH-Test")
    with pytest.raises(PolicyError):
        enabled.get_vm("Other")


def test_allowlist_is_case_insensitive(enabled: HyperVService, backend: FakeBackend) -> None:
    enabled.power("north-test", "start")
    assert backend.calls[-1] == ("vm_power", {"vm": "north-test", "action": "start"})


@pytest.mark.parametrize("action", ["turn_off", "reset"])
def test_forced_power_needs_exact_confirmation(
    enabled: HyperVService, backend: FakeBackend, action: str
) -> None:
    with pytest.raises(PolicyError):
        service_call = getattr(enabled, "power")
        service_call("NORTH-Test", action, "wrong")
    assert backend.calls == []
    enabled.power("NORTH-Test", action, "NORTH-Test")
    assert backend.calls[-1][1]["action"] == action


def test_checkpoint_restore_requires_vm_and_id(
    enabled: HyperVService, backend: FakeBackend
) -> None:
    snapshot_id = "91A910B9-7036-45F6-AC3C-8806E80921E9"
    with pytest.raises(PolicyError):
        enabled.restore_checkpoint("NORTH-Test", snapshot_id, "NORTH-Test", "wrong")
    assert backend.calls == []
    enabled.restore_checkpoint("NORTH-Test", snapshot_id, "NORTH-Test", snapshot_id)
    assert backend.calls[-1][1]["snapshot_id"] == snapshot_id


def test_guest_password_only_goes_over_bridge_payload(
    enabled: HyperVService, backend: FakeBackend
) -> None:
    enabled.guest_exec("NORTH-Test", "Write-Output 'ok'")
    operation, payload = backend.calls[-1]
    assert operation == "guest_exec"
    assert payload["username"] == "Administrator"
    assert payload["password"] == "secret"


def test_missing_guest_credentials_fails_before_execution(backend: FakeBackend) -> None:
    service = HyperVService(
        backend,
        Settings(
            allowed_vms=frozenset({"NORTH-Test"}),
            allow_guest=True,
            guest_username="Administrator",
            guest_password=None,
        ),
    )
    with pytest.raises(PolicyError):
        service.guest_exec("NORTH-Test", "hostname")
    assert backend.calls == []


def test_guest_write_refuses_overwrite_by_default(
    enabled: HyperVService, backend: FakeBackend
) -> None:
    import base64

    data = base64.b64encode(b"hello").decode()
    with pytest.raises(PolicyError):
        enabled.guest_write_file("NORTH-Test", r"C:\Lab\a.txt", data, overwrite=True)
    assert backend.calls == []
    enabled.guest_write_file(
        "NORTH-Test", r"C:\Lab\a.txt", data, overwrite=True, confirm_path=r"C:\Lab\a.txt"
    )
    assert backend.calls[-1][1]["content_base64"] == data


@pytest.mark.parametrize("invalid", ["hello", "!!!!", "YQ== garbage"])
def test_invalid_base64_is_rejected(
    enabled: HyperVService, backend: FakeBackend, invalid: str
) -> None:
    with pytest.raises(ValidationError):
        enabled.guest_write_file("NORTH-Test", r"C:\Lab\a.txt", invalid)
    assert backend.calls == []


def test_console_does_not_audit_typed_text(enabled: HyperVService, backend: FakeBackend) -> None:
    enabled.console_type_text("NORTH-Test", "sensitive text")
    assert backend.calls[-1][1]["text"] == "sensitive text"
    enabled.console_press_keys("NORTH-Test", ["ctrl", "s"])
    assert backend.calls[-1][1]["keys"] == ["ctrl", "s"]
    with pytest.raises(ValidationError):
        enabled.console_press_keys("NORTH-Test", ["ctrl", "ctrl"])


def test_mouse_coordinates_must_be_a_pair(
    enabled: HyperVService, backend: FakeBackend
) -> None:
    with pytest.raises(ValidationError):
        enabled.console_click("NORTH-Test", x=10)
    with pytest.raises(ValidationError):
        enabled.console_click("NORTH-Test", x=-1, y=10)
    enabled.console_click("NORTH-Test", "left", 100, 200)
    assert backend.calls[-1][1] == {
        "vm": "NORTH-Test", "button_index": 1, "x": 100, "y": 200
    }


def test_debug_serial_prepare_requires_confirmation_without_guest_credentials(
    enabled: HyperVService, backend: FakeBackend
) -> None:
    with pytest.raises(PolicyError):
        enabled.debug_serial_prepare("NORTH-Test", "north-kd", "wrong")
    assert backend.calls == []
    enabled.debug_serial_prepare("NORTH-Test", "north-kd", "NORTH-Test")
    assert backend.calls[-1][1] == {"vm": "NORTH-Test", "pipe_name": "north-kd"}


def test_debug_guest_prepare_is_separate_and_uses_credentials(
    enabled: HyperVService, backend: FakeBackend
) -> None:
    with pytest.raises(PolicyError):
        enabled.debug_guest_prepare("NORTH-Test", "wrong")
    enabled.debug_guest_prepare("NORTH-Test", "NORTH-Test")
    assert backend.calls[-1] == (
        "debug_guest_prepare",
        {"vm": "NORTH-Test", "username": "Administrator", "password": "secret"},
    )


def test_audit_is_metadata_only(
    enabled: HyperVService, backend: FakeBackend, tmp_path
) -> None:
    log = tmp_path / "audit.jsonl"
    enabled.audit = AuditLogger(log)
    enabled.console_type_text("NORTH-Test", "do not log this")
    enabled.guest_exec("NORTH-Test", "secret command")
    contents = log.read_text(encoding="utf-8")
    assert "do not log this" not in contents
    assert "secret command" not in contents
    assert "secret" not in contents
    assert "NORTH-Test" in contents


def test_backend_failure_is_audited(
    enabled: HyperVService, backend: FakeBackend, tmp_path
) -> None:
    log = tmp_path / "audit.jsonl"
    enabled.audit = AuditLogger(log)
    backend.failure = BackendError("Access denied", code="access_denied")
    with pytest.raises(BackendError):
        enabled.power("NORTH-Test", "start")
    assert "access_denied" in log.read_text(encoding="utf-8")


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_pause_and_resume_use_existing_power_gate(
    enabled: HyperVService, backend: FakeBackend, action: str
) -> None:
    enabled.power("NORTH-Test", action)
    assert backend.calls[-1] == ("vm_power", {"vm": "NORTH-Test", "action": action})


def test_vm_wait_matches_without_power_action(
    enabled: HyperVService, backend: FakeBackend
) -> None:
    states = iter(["Off", "Running"])
    original_invoke = backend.invoke

    def invoke(operation, payload, *, timeout=None):
        if operation == "vm_get":
            backend.calls.append((operation, dict(payload)))
            return {"vm": {"state": next(states)}}
        return original_invoke(operation, payload, timeout=timeout)

    backend.invoke = invoke
    result = enabled.vm_wait("NORTH-Test", ["running"], poll_interval_ms=100)
    assert result["matched"] is True
    assert result["polls"] == 2
    assert {operation for operation, _ in backend.calls} == {"vm_get"}


def test_vm_wait_timeout_does_not_retry_power(
    enabled: HyperVService, backend: FakeBackend, monkeypatch
) -> None:
    backend.responses["vm_get"] = {"vm": {"state": "Off"}}
    clock = iter([0.0, 0.5, 2.0])
    monkeypatch.setattr("hyperv_console_mcp.service.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("hyperv_console_mcp.service.time.sleep", lambda _: None)
    result = enabled.vm_wait("NORTH-Test", ["Running"], timeout_seconds=1)
    assert result["matched"] is False
    assert {operation for operation, _ in backend.calls} == {"vm_get"}


def test_guest_diagnostics_keep_credentials_out_of_tool_results(
    enabled: HyperVService, backend: FakeBackend
) -> None:
    enabled.guest_status("NORTH-Test")
    enabled.guest_processes("NORTH-Test", "arc")
    enabled.guest_services("NORTH-Test", "win", include_stopped=True)
    enabled.guest_events("NORTH-Test", "System", event_id=41, level=2)
    assert [call[0] for call in backend.calls[-4:]] == [
        "guest_status", "guest_processes", "guest_services", "guest_events"
    ]
    assert all(call[1]["password"] == "secret" for call in backend.calls[-4:])
    assert backend.calls[-1][1]["event_id"] == 41
    before = len(backend.calls)
    with pytest.raises(ValidationError):
        enabled.guest_events("NORTH-Test", "ForwardedEvents")
    assert len(backend.calls) == before


def test_console_key_mouse_and_batch_validation(
    enabled: HyperVService, backend: FakeBackend
) -> None:
    enabled.console_key_state("NORTH-Test", "shift", True)
    enabled.console_mouse_buttons("NORTH-Test", left=True)
    result = enabled.console_batch(
        "NORTH-Test",
        [{"type": "move", "x": 10, "y": 20}, {"type": "wait", "milliseconds": 1}],
    )
    assert result["actions_completed"] == 2
    before = len(backend.calls)
    with pytest.raises(ValidationError):
        enabled.console_batch(
            "NORTH-Test",
            [{"type": "text", "text": "would otherwise run"}, {"type": "guest_exec", "command": "bad"}],
        )
    assert len(backend.calls) == before
    with pytest.raises(ValidationError):
        enabled.console_key_state("NORTH-Test", "shift", 1)


def test_media_requires_confirmation_and_offline_safe_arguments(
    enabled: HyperVService, backend: FakeBackend
) -> None:
    with pytest.raises(PolicyError):
        enabled.media_mount("NORTH-Test", r"C:\Lab\boot.iso", 1, "wrong")
    assert backend.calls == []
    enabled.media_mount("NORTH-Test", r"C:\Lab\boot.iso", 1, "NORTH-Test", True)
    assert backend.calls[-1][1] == {
        "vm": "NORTH-Test", "path": r"C:\Lab\boot.iso", "controller_number": 0,
        "controller_location": 1, "boot_first": True,
    }
    enabled.media_eject("NORTH-Test", 1, "NORTH-Test")
    assert backend.calls[-1][0] == "media_eject"
    before = len(backend.calls)
    with pytest.raises(ValidationError):
        enabled.media_mount("NORTH-Test", r"C:\Lab\boot.vhdx", 1, "NORTH-Test")
    assert len(backend.calls) == before


class FakeSerial:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def status(self, vm_name: str) -> dict[str, Any]:
        return {"connected": bool(self.calls)}

    def connect(self, vm_name: str, pipe_name: str, log_path: str) -> dict[str, Any]:
        self.calls.append(("connect", vm_name, pipe_name, log_path))
        return {"connected": True}

    def read(self, vm_name: str, tail: int) -> dict[str, Any]:
        self.calls.append(("read", vm_name, tail))
        return {"text": "guest output", "connected": True}

    def send(self, vm_name: str, text: str) -> dict[str, Any]:
        self.calls.append(("send", vm_name, text))
        return {"bytes_sent": len(text.encode()), "completed": True}

    def disconnect(self, vm_name: str) -> dict[str, Any]:
        self.calls.append(("disconnect", vm_name))
        return {"connected": False, "completed": True}


def test_serial_requires_matching_com1_and_never_sends_text_to_bridge(
    enabled: HyperVService, backend: FakeBackend
) -> None:
    serial = FakeSerial()
    service = HyperVService(backend, enabled.settings, serial=serial)
    backend.responses["debug_serial_status"] = {
        "state": "Running",
        "com_ports": [{"number": 1, "path": r"\\.\pipe\north-console"}],
    }
    with pytest.raises(ValidationError):
        service.serial_connect("NORTH-Test", "wrong", "console.log", "NORTH-Test")
    assert serial.calls == []
    service.serial_connect("NORTH-Test", "north-console", "console.log", "NORTH-Test")
    service.serial_send("NORTH-Test", "secret serial text")
    result = service.serial_read("NORTH-Test")
    service.serial_disconnect("NORTH-Test")
    assert result["text"] == "guest output"
    assert serial.calls[1] == ("send", "NORTH-Test", "secret serial text\r\n")
    assert all("secret serial text" not in repr(call) for call in backend.calls)
    assert {call[0] for call in backend.calls} == {"debug_serial_status"}
