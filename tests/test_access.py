"""Access levels: one word per VM, enforced where the capability flags were."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.access import (
    CAPABILITIES,
    LEVEL_CAPABILITIES,
    AccessStore,
    access_file,
    normalise_level,
)
from hyperv_console_mcp.config import CAPABILITY_VARIABLES, Settings
from hyperv_console_mcp.errors import PolicyError, ValidationError
from hyperv_console_mcp.service import HyperVService


class RecordingBackend:
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


def _service(level: str | None, backend: RecordingBackend | None = None) -> HyperVService:
    settings = Settings(
        allowed_vms=frozenset({"Lab-VM"}),
        levels={"lab-vm": level} if level else {},
        guest_username="Administrator",
        guest_password="secret",
        iso_root=Path("C:/Lab"),
    )
    return HyperVService(backend or RecordingBackend(), settings)


def test_a_level_is_the_set_of_capabilities_it_names() -> None:
    assert LEVEL_CAPABILITIES["read-only"] == frozenset()
    assert LEVEL_CAPABILITIES["control"] == frozenset(
        {"power", "checkpoints", "console", "screenshots", "media"}
    )
    # The line between control and lab is code and a debugger inside the guest.
    assert "guest" not in LEVEL_CAPABILITIES["control"]
    assert "debugger" not in LEVEL_CAPABILITIES["control"]
    assert LEVEL_CAPABILITIES["lab"] == frozenset(CAPABILITIES)


def test_a_read_only_vm_is_listed_but_never_changed() -> None:
    backend = RecordingBackend()
    service = _service("read-only", backend)
    assert service.list_vms()["vms"] is not None
    with pytest.raises(PolicyError) as caught:
        service.power("Lab-VM", "start")
    assert "read-only" in str(caught.value)
    assert backend.calls[-1][0] == "vm_list"


def test_control_runs_the_vm_but_does_not_reach_into_the_guest() -> None:
    backend = RecordingBackend()
    service = _service("control", backend)
    service.power("Lab-VM", "start")
    assert backend.calls[-1][0] == "vm_power"
    with pytest.raises(PolicyError) as caught:
        service.guest_exec("Lab-VM", "hostname")
    assert "does not include guest" in str(caught.value)


def test_lab_reaches_the_guest() -> None:
    backend = RecordingBackend()
    service = _service("lab", backend)
    assert service.guest_exec("Lab-VM", "hostname")["execution"]["success"] is True
    assert backend.calls[-1][0] == "guest_exec"


def test_a_level_wins_over_the_host_wide_flags() -> None:
    """Per-VM is more specific than a variable that speaks for every VM."""
    settings = Settings(
        allowed_vms=frozenset({"Lab-VM"}),
        levels={"lab-vm": "read-only"},
        allow_power=True,
    )
    assert settings.allows("Lab-VM", "power") is False
    assert settings.capabilities_for("Lab-VM") == frozenset()


def test_a_vm_without_a_level_keeps_the_older_flags() -> None:
    settings = Settings(allowed_vms=frozenset({"Lab-VM"}), allow_power=True, allow_media=True)
    assert settings.allows("Lab-VM", "power") is True
    assert settings.allows("Lab-VM", "media") is True
    assert settings.allows("Lab-VM", "guest") is False


def test_the_refusal_says_which_level_and_how_to_raise_it() -> None:
    settings = Settings(allowed_vms=frozenset({"Lab-VM"}), levels={"lab-vm": "control"})
    message = settings.refusal("Lab-VM", "guest")
    assert "'control'" in message
    assert 'hyperv-access set "Lab-VM" control|lab' in message


def test_the_refusal_falls_back_to_the_variable_when_no_level_is_set() -> None:
    settings = Settings(allowed_vms=frozenset({"Lab-VM"}))
    assert "HYPERV_MCP_ALLOW_POWER=true" in settings.refusal("Lab-VM", "power")


def test_levels_round_trip_through_the_file(tmp_path: Path) -> None:
    store = AccessStore(tmp_path / "access.json")
    store.set("Lab-VM", "lab")
    store.set("Prod-VM", "read-only")
    assert store.levels() == {"lab-vm": "lab", "prod-vm": "read-only"}
    assert store.load()["lab-vm"] == ("Lab-VM", "lab")
    store.remove("PROD-VM")
    assert store.levels() == {"lab-vm": "lab"}
    assert not (tmp_path / "access.json.tmp").exists()


def test_the_file_supplies_the_allowlist_when_the_environment_names_none(tmp_path: Path) -> None:
    path = tmp_path / "access.json"
    AccessStore(path).set("Lab-VM", "lab")
    settings = Settings.from_env({"HYPERV_MCP_ACCESS_FILE": str(path)})
    assert settings.allowed_vms == frozenset({"Lab-VM"})
    assert settings.level_for("lab-vm") == "lab"
    assert settings.allows("Lab-VM", "debugger") is True


def test_an_environment_allowlist_narrows_the_file(tmp_path: Path) -> None:
    path = tmp_path / "access.json"
    AccessStore(path).set("Lab-VM", "lab")
    AccessStore(path).set("Prod-VM", "lab")
    settings = Settings.from_env({
        "HYPERV_MCP_ACCESS_FILE": str(path),
        "HYPERV_MCP_ALLOWED_VMS": "Lab-VM",
    })
    assert settings.allowed_vms == frozenset({"Lab-VM"})
    assert settings.is_allowed("Prod-VM") is False
    # The level is still there, so narrowing the allowlist does not lose it.
    assert settings.level_for("prod-vm") == "lab"


def test_a_level_set_while_the_service_runs_takes_effect_on_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The level is chosen from the window, so the host must read the file again."""
    path = tmp_path / "access.json"
    monkeypatch.setenv("HYPERV_MCP_ACCESS_FILE", str(path))
    for name in ("HYPERV_MCP_ALLOWED_VMS", "HYPERV_MCP_PROFILE", "HYPERV_MCP_TOOLSETS"):
        monkeypatch.delenv(name, raising=False)
    for name in CAPABILITY_VARIABLES:
        monkeypatch.delenv(name, raising=False)

    service = HyperVService(RecordingBackend(), Settings.from_env())
    assert service.settings.allowed_vms == frozenset()
    AccessStore(path).set("Lab-VM", "control")

    policy = service.reload_policy()

    assert policy == {"levels": {"lab-vm": "control"}, "allowlist": ["Lab-VM"]}
    assert service.settings.allows("Lab-VM", "power") is True
    assert service.settings.allows("Lab-VM", "guest") is False


def test_the_cli_tells_a_running_service_and_never_needs_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A level set in a terminal must not wait for a restart, and no host is no error."""
    from uuid import uuid4

    from hyperv_console_mcp.access_cli import _notify_service

    monkeypatch.setenv(
        "HYPERV_MCP_SERVICE_PIPE", rf"\\.\pipe\HyperVConsoleMCP-Service-absent-{uuid4().hex}"
    )
    result: dict[str, Any] = {}
    _notify_service(result)
    assert result == {"service": "not running"}


def test_a_broken_file_is_an_error_not_an_empty_policy(tmp_path: Path) -> None:
    path = tmp_path / "access.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationError, match="could not be read"):
        Settings.from_env({"HYPERV_MCP_ACCESS_FILE": str(path)})


def test_an_unknown_level_in_the_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "access.json"
    path.write_text(json.dumps({"version": 1, "vms": {"vm": "god-mode"}}), encoding="utf-8")
    with pytest.raises(ValidationError, match="read-only, control, lab"):
        Settings.from_env({"HYPERV_MCP_ACCESS_FILE": str(path)})


def test_a_relative_access_file_is_refused() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        access_file({"HYPERV_MCP_ACCESS_FILE": "access.json"})


def test_a_level_name_is_case_insensitive() -> None:
    assert normalise_level(" Lab ") == "lab"
    with pytest.raises(ValidationError):
        normalise_level("")
