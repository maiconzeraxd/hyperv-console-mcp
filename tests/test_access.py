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


def test_a_baseline_is_stored_beside_the_level_and_survives_a_level_change(tmp_path: Path) -> None:
    """The baseline is the operator's, so changing what the AI may do keeps it."""
    store = AccessStore(tmp_path / "access.json")
    store.set("Lab-VM", "lab")
    store.set_baseline("lab-vm", "golden")
    assert store.baselines() == {"lab-vm": "golden"}

    store.set("Lab-VM", "control")
    assert store.baselines() == {"lab-vm": "golden"}
    stored = json.loads((tmp_path / "access.json").read_text(encoding="utf-8"))
    assert stored["vms"]["lab-vm"]["baseline"] == "golden"
    assert stored["vms"]["lab-vm"]["level"] == "control"


def test_a_baseline_needs_a_level_to_belong_to(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        AccessStore(tmp_path / "access.json").set_baseline("Lab-VM", "golden")


def test_clearing_the_baseline_leaves_the_level(tmp_path: Path) -> None:
    store = AccessStore(tmp_path / "access.json")
    store.set("Lab-VM", "lab")
    store.set_baseline("Lab-VM", "golden")
    store.set_baseline("Lab-VM", "")
    assert store.baselines() == {}
    assert store.levels() == {"lab-vm": "lab"}


def test_a_file_that_still_holds_a_bare_level_loads(tmp_path: Path) -> None:
    """The first version wrote one string per VM; those files keep working."""
    path = tmp_path / "access.json"
    path.write_text(json.dumps({"version": 1, "vms": {"lab-vm": "lab"}}), encoding="utf-8")
    store = AccessStore(path)
    assert store.levels() == {"lab-vm": "lab"}
    assert store.baselines() == {}


def test_a_baseline_reaches_the_settings(tmp_path: Path) -> None:
    path = tmp_path / "access.json"
    store = AccessStore(path)
    store.set("NORTH-Test", "control")
    store.set_baseline("NORTH-Test", "golden")
    settings = Settings.from_env({"HYPERV_MCP_ACCESS_FILE": str(path)})
    assert settings.baseline_for("north-test") == "golden"


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


def test_the_cli_reports_the_records_it_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """set/remove return full records, not load()'s two-item compatibility tuples."""
    from uuid import uuid4

    from hyperv_console_mcp import access_cli

    monkeypatch.setenv("HYPERV_MCP_ACCESS_FILE", str(tmp_path / "access.json"))
    monkeypatch.setenv(
        "HYPERV_MCP_SERVICE_PIPE", rf"\\.\pipe\HyperVConsoleMCP-Service-absent-{uuid4().hex}"
    )
    monkeypatch.setattr("sys.argv", ["hyperv-access", "set", "NORTH-Test", "lab"])

    access_cli.main()

    result = json.loads(capsys.readouterr().out)
    assert result["vms"] == [{
        "name": "NORTH-Test",
        "level": "lab",
        "capabilities": sorted(LEVEL_CAPABILITIES["lab"]),
    }]
    assert result["service"] == "not running"


def test_the_cli_lists_a_file_it_did_not_just_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """list reads the saved file, whose load() tuples are not report records."""
    from uuid import uuid4

    from hyperv_console_mcp import access_cli

    path = tmp_path / "access.json"
    AccessStore(path).set("NORTH-Test", "lab")
    monkeypatch.setenv("HYPERV_MCP_ACCESS_FILE", str(path))
    monkeypatch.setenv(
        "HYPERV_MCP_SERVICE_PIPE", rf"\\.\pipe\HyperVConsoleMCP-Service-absent-{uuid4().hex}"
    )
    monkeypatch.setattr("sys.argv", ["hyperv-access", "list"])

    access_cli.main()

    result = json.loads(capsys.readouterr().out)
    assert result["vms"] == [{
        "name": "NORTH-Test",
        "level": "lab",
        "capabilities": sorted(LEVEL_CAPABILITIES["lab"]),
    }]


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

def test_a_level_can_be_narrowed_to_a_subset_of_its_capabilities(tmp_path: Path) -> None:
    """The matrix: the level is the ceiling, and the operator can take less.

    A grid of "what I allow" is only honest if unchecking something removes it
    from what the AI may do — and if checking something the level never had is
    refused rather than quietly widening the level.
    """
    store = AccessStore(tmp_path / "access.json")

    store.set("NORTH-Test", "lab", ["guest", "console"])

    record = store.entries()["north-test"]
    assert record["level"] == "lab"
    assert record["capabilities"] == ["console", "guest"]


def test_a_capability_outside_the_level_is_refused(tmp_path: Path) -> None:
    store = AccessStore(tmp_path / "access.json")

    with pytest.raises(ValidationError, match="read-only"):
        store.set("NORTH-Test", "read-only", ["guest"])
    with pytest.raises(ValidationError, match="unknown capability"):
        store.set("NORTH-Test", "lab", ["root"])


def test_setting_a_level_again_clears_the_narrowing(tmp_path: Path) -> None:
    """A level is the whole statement; a stale subset must not survive it."""
    store = AccessStore(tmp_path / "access.json")
    store.set("NORTH-Test", "lab", ["guest"])

    store.set("NORTH-Test", "lab")

    assert store.entries()["north-test"]["capabilities"] is None


def test_the_policy_honours_the_narrowing(tmp_path: Path) -> None:
    path = tmp_path / "access.json"
    AccessStore(path).set("NORTH-Test", "lab", ["console"])

    settings = Settings.from_env({"HYPERV_MCP_ACCESS_FILE": str(path)})

    assert settings.level_for("NORTH-Test") == "lab"
    assert settings.allows("NORTH-Test", "console") is True
    assert settings.allows("NORTH-Test", "guest") is False
    assert settings.allows("NORTH-Test", "debugger") is False
    # The refusal names the level, which is what the operator can change.
    assert "lab" in settings.refusal("NORTH-Test", "guest")


def test_a_file_that_widens_its_own_level_is_refused(tmp_path: Path) -> None:
    """A hand-edited file cannot grant past its level, and says so loudly.

    The house rule is loud rather than lenient: a policy file that asks for
    something impossible stops with a message instead of being silently
    narrowed into a policy the operator did not write.
    """
    path = tmp_path / "access.json"
    path.write_text(json.dumps({
        "version": 1,
        "vms": {"north-test": {
            "name": "NORTH-Test", "level": "control",
            "capabilities": ["guest", "power"],
        }},
    }), encoding="utf-8")

    with pytest.raises(ValidationError, match="never widen"):
        Settings.from_env({"HYPERV_MCP_ACCESS_FILE": str(path)})


def test_a_hand_built_mapping_cannot_widen_a_level() -> None:
    """Defence in depth: the policy intersects, whatever it was handed."""
    settings = Settings(
        levels={"north-test": "control"},
        capabilities={"north-test": ("power", "guest")},
    )

    assert settings.allows("NORTH-Test", "power") is True
    assert settings.allows("NORTH-Test", "guest") is False

def test_the_cli_narrows_a_level_in_one_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--capabilities`` is the matrix without a window open."""
    from uuid import uuid4

    from hyperv_console_mcp import access_cli

    monkeypatch.setenv("HYPERV_MCP_ACCESS_FILE", str(tmp_path / "access.json"))
    monkeypatch.setenv(
        "HYPERV_MCP_SERVICE_PIPE", rf"\\.\pipe\HyperVConsoleMCP-Service-absent-{uuid4().hex}"
    )
    monkeypatch.setattr(
        "sys.argv",
        ["hyperv-access", "set", "NORTH-Test", "lab", "--capabilities", "guest, console"],
    )

    access_cli.main()

    result = json.loads(capsys.readouterr().out)
    assert result["vms"] == [{
        "name": "NORTH-Test",
        "level": "lab",
        "capabilities": ["console", "guest"],
    }]

def test_a_narrowed_capability_says_it_was_unchecked_not_out_of_reach(tmp_path: Path) -> None:
    """The refusal must name the thing the operator can actually change.

    Telling them the level does not include ``guest`` sends them to raise a
    level that already had it; the matrix is what took it away.
    """
    path = tmp_path / "access.json"
    AccessStore(path).set("NORTH-Test", "lab", ["console"])
    settings = Settings.from_env({"HYPERV_MCP_ACCESS_FILE": str(path)})

    message = settings.refusal("NORTH-Test", "guest")

    assert "unchecked" in message
    assert "does not include" not in message
    # And a level that never had it still says so.
    AccessStore(path).set("NORTH-Test", "control")
    assert "does not include" in Settings.from_env(
        {"HYPERV_MCP_ACCESS_FILE": str(path)}
    ).refusal("NORTH-Test", "guest")

