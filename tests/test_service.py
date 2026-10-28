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

    def invoke(
        self, operation: str, payload: Mapping[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((operation, dict(payload)))
        if self.failure:
            raise self.failure
        if operation == "vm_list":
            return {"vms": [{"name": "NORTH-Test"}, {"name": "Other"}]}
        return {"operation": operation}


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


def test_debug_prepare_requires_confirmation_and_credentials(
    enabled: HyperVService, backend: FakeBackend
) -> None:
    with pytest.raises(PolicyError):
        enabled.debug_serial_prepare("NORTH-Test", "north-kd", "wrong", True)
    assert backend.calls == []
    enabled.debug_serial_prepare("NORTH-Test", "north-kd", "NORTH-Test", True)
    assert backend.calls[-1][1] == {
        "vm": "NORTH-Test",
        "pipe_name": "north-kd",
        "configure_guest": True,
        "username": "Administrator",
        "password": "secret",
    }


def test_debug_prepare_without_guest_does_not_send_credentials(
    enabled: HyperVService, backend: FakeBackend
) -> None:
    enabled.debug_serial_prepare("NORTH-Test", "north-kd", "NORTH-Test")
    assert "password" not in backend.calls[-1][1]


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
