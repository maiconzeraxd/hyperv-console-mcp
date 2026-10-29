"""Firmware: the generation, the Secure Boot flag, and the shape for generation 1."""

from __future__ import annotations

from typing import Any

import pytest

from hyperv_console_mcp.hostwmi import WmiBackend
from hyperv_console_mcp.hostwmi.connection import WmiFailure
from hyperv_console_mcp.hostwmi.firmware import vm_firmware, vm_secure_boot

VM_ID = "D5AD7D1D-5CB3-441F-A9D7-138640C8F61A"


class FakeConnection:
    def __init__(
        self, *, generation: int = 2, secure_boot: bool = False, state: int = 2, settings: int = 1
    ) -> None:
        self.generation = generation
        self.secure_boot = secure_boot
        self.state = state
        self.settings = settings
        self.queries: list[str] = []

    def query(self, wql: str, *, namespace: str = "") -> list[dict[str, Any]]:
        self.queries.append(wql)
        if "FROM Msvm_ComputerSystem" in wql:
            return [
                {
                    "ElementName": "NORTH-Test",
                    "Name": VM_ID,
                    "EnabledState": self.state,
                    "StatusDescriptions": [],
                    "OnTimeInMilliseconds": 0,
                }
            ]
        if "System:Realized" in wql:
            return [
                {
                    "InstanceID": f"Microsoft:{VM_ID}",
                    "VirtualSystemSubType": f"Microsoft:Hyper-V:SubType:{self.generation}",
                    "SecureBootEnabled": self.secure_boot,
                }
                for _ in range(self.settings)
            ]
        return [{"__PATH": "x"}]

    def get(self, path: str, *, namespace: str = "") -> dict[str, Any]:
        raise AssertionError("reading firmware needs no Get")

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        raise AssertionError("reading firmware needs no method call")


def test_a_generation_two_vm_reports_secure_boot_off() -> None:
    result = vm_firmware(FakeConnection(), {"vm": "NORTH-Test"})
    assert result == {
        "vm": "NORTH-Test",
        "generation": 2,
        "state": "Running",
        "secure_boot_supported": True,
        "secure_boot": "Off",
        "preferred_boot_device": None,
    }


def test_secure_boot_on_is_the_same_string_the_cmdlet_prints() -> None:
    result = vm_firmware(FakeConnection(secure_boot=True), {"vm": "NORTH-Test"})
    assert result["secure_boot"] == "On"


def test_a_generation_one_vm_supports_no_secure_boot() -> None:
    """Get-VMFirmware refuses generation 1; the bridge answers with a flag instead."""
    result = vm_firmware(FakeConnection(generation=1, secure_boot=True), {"vm": "NORTH-Test"})
    assert (result["generation"], result["secure_boot_supported"]) == (1, False)
    assert result["secure_boot"] is None


def test_the_setting_query_asks_for_two_fields_only() -> None:
    """The provider charges per property read, so a SELECT * is not free."""
    connection = FakeConnection()
    vm_firmware(connection, {"vm": "NORTH-Test"})
    query = next(q for q in connection.queries if "System:Realized" in q)
    assert "SecureBootEnabled" in query
    assert "*" not in query


def test_a_missing_setting_is_an_error() -> None:
    with pytest.raises(WmiFailure) as caught:
        vm_firmware(FakeConnection(settings=0), {"vm": "NORTH-Test"})
    assert caught.value.code == "backend_error"


def test_the_backend_serves_vm_firmware() -> None:
    backend = WmiBackend(connect=FakeConnection)
    assert backend.invoke("vm_firmware", {"vm": "NORTH-Test"})["generation"] == 2


class WritingConnection(FakeConnection):
    """A provider that applies ``ModifySystemSettings``, unless told not to."""

    def __init__(self, *, apply: bool = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.apply = apply
        self.calls: list[dict[str, Any]] = []

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        self.calls.append({"method": method, **parameters})
        if self.apply:
            self.secure_boot = "<VALUE>TRUE</VALUE>" in parameters["SystemSettings"]
        return {"ReturnValue": 0}


def test_secure_boot_is_a_modify_system_settings() -> None:
    connection = WritingConnection(state=3)
    vm_secure_boot(connection, {"vm": "NORTH-Test", "enable": True})
    call = connection.calls[0]
    assert call["method"] == "ModifySystemSettings"
    assert f"Microsoft:{VM_ID}" in call["SystemSettings"]
    assert "<VALUE>TRUE</VALUE>" in call["SystemSettings"]


def test_secure_boot_off_sends_false() -> None:
    connection = WritingConnection(state=3, secure_boot=True)
    result = vm_secure_boot(connection, {"vm": "NORTH-Test", "enable": False})
    assert "<VALUE>FALSE</VALUE>" in connection.calls[0]["SystemSettings"]
    assert result["secure_boot"] == "Off"


def test_the_answer_is_read_back_not_echoed() -> None:
    """``enabled`` is what was asked; ``secure_boot`` is what the VM now says."""
    connection = WritingConnection(state=3, apply=False)
    result = vm_secure_boot(connection, {"vm": "NORTH-Test", "enable": True})
    assert (result["enabled"], result["secure_boot"]) == (True, "Off")


def test_a_generation_one_vm_is_refused() -> None:
    connection = WritingConnection(state=3, generation=1)
    with pytest.raises(WmiFailure) as caught:
        vm_secure_boot(connection, {"vm": "NORTH-Test", "enable": True})
    assert caught.value.code == "invalid_argument"
    assert connection.calls == []


def test_a_running_vm_is_refused() -> None:
    connection = WritingConnection(state=2)
    with pytest.raises(WmiFailure) as caught:
        vm_secure_boot(connection, {"vm": "NORTH-Test", "enable": True})
    assert caught.value.code == "invalid_state"
    assert connection.calls == []


def test_the_backend_serves_vm_secure_boot() -> None:
    backend = WmiBackend(connect=lambda: WritingConnection(state=3))
    assert backend.invoke("vm_secure_boot", {"vm": "NORTH-Test", "enable": True})["secure_boot"] == "On"
