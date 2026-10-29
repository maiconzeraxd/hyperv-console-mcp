"""KDNET host setup: port allocation, guest BCD command, and key hygiene."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp import kd
from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import BackendError, ValidationError
from hyperv_console_mcp.kd import kdnet


class FakeStore:
    """Stand-in for the Credential Manager: nothing touches the real vault."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def read(self, key: str) -> str | None:
        return self.values.get(key)

    def write(self, key: str, secret: str, *, label: str = "kdnet") -> None:
        self.values[key] = secret

    def delete(self, key: str) -> bool:
        return self.values.pop(key, None) is not None

    def has(self, key: str) -> bool:
        return key in self.values


class FakeExecution:
    def __init__(self, output: str, success: bool = True) -> None:
        self.output = output
        self.success = success


class FakeTransport:
    output = "HVMCP_KDNET_OK:net ... key *** | debug Yes"
    success = True
    instance: FakeTransport | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.commands: list[str] = []
        FakeTransport.instance = self

    def health(self, vm: str) -> dict[str, Any]:
        return {"vm": vm, "healthy": True}

    def exec(self, vm: str, command: str, *, timeout: float, max_output: int) -> FakeExecution:
        self.commands.append(command)
        return FakeExecution(self.output, self.success)


class FakeService:
    backend = object()

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _run(
        self,
        operation: str,
        bridge_operation: str,
        payload: dict[str, Any] | None = None,
        *,
        vm_name: str | None = None,
        audit: bool = True,
    ) -> dict[str, Any]:
        self.calls.append((bridge_operation, dict(payload or {})))
        if bridge_operation == "ssh_network_prepare":
            return {
                "adapter_name": "HyperV-MCP-Lab",
                "adapter_created": True,
                "host_address_created": True,
            }
        if bridge_operation == "kdnet_firewall_apply":
            return {"rule_name": payload["rule_name"], "created": True}
        if bridge_operation == "kdnet_firewall_remove":
            return {"removed": True}
        return {}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        allowed_vms=frozenset({"NORTH-Test"}),
        allow_debugger=True,
        kd_log_root=tmp_path / "kd",
        symbol_root=tmp_path / "symbols",
    )


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kdnet, "find_debugger", lambda *a, **k: Path("C:/Debuggers/kd.exe")
    )
    monkeypatch.setattr(
        kdnet,
        "host_address",
        lambda settings: (ipaddress.ip_network("10.77.0.0/24"), "10.77.0.1"),
    )
    monkeypatch.setattr(kdnet, "PowerShellDirectTransport", FakeTransport)


def test_generate_key_is_four_octets_in_range() -> None:
    keys = {kdnet.generate_key() for _ in range(50)}
    assert len(keys) > 1
    for key in keys:
        parts = key.split(".")
        assert len(parts) == 4
        assert all(1 <= int(part) <= 255 for part in parts)


def test_choose_port_takes_the_lowest_free_port(settings: Settings) -> None:
    assert kdnet.choose_port(settings, "NORTH-Test") == kdnet.PORT_MIN
    kdnet.write_json(
        kdnet.kdnet_state_path(settings, "Other"),
        {"vm": "Other", "port": kdnet.PORT_MIN},
    )
    assert kdnet.choose_port(settings, "NORTH-Test") == kdnet.PORT_MIN + 1


def test_choose_port_refuses_a_port_another_vm_claims(settings: Settings) -> None:
    kdnet.write_json(
        kdnet.kdnet_state_path(settings, "Other"), {"vm": "Other", "port": 50_005}
    )
    with pytest.raises(ValidationError):
        kdnet.choose_port(settings, "NORTH-Test", requested=50_005)
    # The same VM may keep the port it already owns.
    assert kdnet.choose_port(settings, "Other", requested=50_005) == 50_005


def test_choose_port_rejects_a_port_outside_the_range(settings: Settings) -> None:
    with pytest.raises(ValidationError):
        kdnet.choose_port(settings, "NORTH-Test", requested=49_999)
    with pytest.raises(ValidationError):
        kdnet.choose_port(settings, "NORTH-Test", requested=50_040)
    with pytest.raises(ValidationError):
        kdnet.choose_port(settings, "NORTH-Test", requested=True)  # type: ignore[arg-type]


def test_guest_command_masks_the_key_it_sets() -> None:
    command = kdnet.guest_command("10.77.0.1", 50_007, "1.2.3.4")
    assert "hostip:10.77.0.1 port:50007 key:1.2.3.4" in command
    # The echoed dbgsettings are masked before they can leave the guest.
    assert "-replace 'key=\\S+', 'key ***'" in command
    assert "(?m)^key\\s+\\S+" in command
    assert kdnet.OK_MARKER in command


def test_removal_command_turns_debug_off_and_verifies_it() -> None:
    command = kdnet.removal_command()
    assert "bcdedit.exe /debug off" in command
    assert "/deletevalue '{current}' dbgsettings" in command
    assert kdnet.ERROR_MARKER in command


def test_prepare_stores_the_key_and_never_returns_it(
    settings: Settings, patched: None
) -> None:
    service = FakeService(settings)
    store = FakeStore()

    result = kdnet.prepare(service, "NORTH-Test", store=store)  # type: ignore[arg-type]

    key = store.read("NORTH-Test")
    assert key is not None
    assert result["port"] == kdnet.PORT_MIN
    assert result["host_ip"] == "10.77.0.1"
    assert result["key_stored"] == "credential_manager"
    assert result["guest_dbgsettings"] == "net ... key *** | debug Yes"
    assert result["reboot_required"] is True
    assert key not in json.dumps(result)

    state_path = kdnet.kdnet_state_path(settings, "NORTH-Test")
    assert key not in state_path.read_text(encoding="utf-8")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["port"] == kdnet.PORT_MIN
    assert state["firewall_applied"] is True
    assert state["key_target"] == "HyperVConsoleMCP/kdnet/NORTH-Test"

    bridge_ops = [operation for operation, _ in service.calls]
    assert bridge_ops == ["ssh_network_prepare", "kdnet_firewall_apply"]
    firewall = service.calls[1][1]
    assert firewall["port"] == kdnet.PORT_MIN
    assert firewall["program"] == "C:\\Debuggers\\kd.exe" or firewall["program"].endswith(
        "kd.exe"
    )
    assert firewall["interface_alias"] == "vEthernet (HyperV-MCP-Lab)"


def test_prepare_deletes_the_key_when_the_guest_refuses(
    settings: Settings, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = FakeService(settings)
    store = FakeStore()

    monkeypatch.setattr(FakeTransport, "success", False)
    monkeypatch.setattr(FakeTransport, "output", "HVMCP_KDNET_ERROR:bcdedit failed")

    with pytest.raises(BackendError):
        kdnet.prepare(service, "NORTH-Test", store=store)  # type: ignore[arg-type]
    assert store.values == {}


def test_deprovision_removes_rule_key_and_state(
    settings: Settings, patched: None
) -> None:
    service = FakeService(settings)
    store = FakeStore()
    kdnet.prepare(service, "NORTH-Test", store=store)  # type: ignore[arg-type]

    result = kdnet.deprovision(service, "NORTH-Test", store=store)  # type: ignore[arg-type]

    assert result["firewall_removed"] is True
    assert result["key_deleted"] is True
    assert store.values == {}
    assert not kdnet.kdnet_state_path(settings, "NORTH-Test").exists()
    assert service.calls[-1][0] == "kdnet_firewall_remove"


def test_status_never_includes_the_key(settings: Settings, patched: None) -> None:
    service = FakeService(settings)
    store = FakeStore()
    kdnet.prepare(service, "NORTH-Test", store=store)  # type: ignore[arg-type]
    key = store.read("NORTH-Test")

    status = kdnet.status(service, "NORTH-Test", store=store)  # type: ignore[arg-type]

    assert status["prepared"] is True
    assert status["key_stored"] is True
    assert key is not None and key not in json.dumps(status)


def test_the_package_exposes_the_kdnet_module() -> None:
    assert kd.kdnet is kdnet
