from __future__ import annotations

from pathlib import Path

import pytest

from hyperv_console_mcp.config import TOOLSETS, Settings
from hyperv_console_mcp.errors import ValidationError


def test_lab_roots_default_under_localappdata(tmp_path: Path) -> None:
    settings = Settings.from_env({"LOCALAPPDATA": str(tmp_path)})
    assert settings.lab_root == tmp_path / "HyperVConsoleMCP" / "lab" / "packages"
    assert settings.evidence_root == tmp_path / "HyperVConsoleMCP" / "evidence"
    assert settings.event_directory == tmp_path / "HyperVConsoleMCP" / "events"


def test_lab_roots_honour_absolute_environment_overrides(tmp_path: Path) -> None:
    settings = Settings.from_env({
        "LOCALAPPDATA": str(tmp_path),
        "HYPERV_MCP_LAB_ROOT": str(tmp_path / "packages"),
        "HYPERV_MCP_EVIDENCE_ROOT": str(tmp_path / "evidence"),
        "HYPERV_MCP_EVENT_DIRECTORY": str(tmp_path / "events"),
    })
    assert settings.lab_root == tmp_path / "packages"
    assert settings.evidence_root == tmp_path / "evidence"
    assert settings.event_directory == tmp_path / "events"


@pytest.mark.parametrize(
    "name",
    ["HYPERV_MCP_LAB_ROOT", "HYPERV_MCP_EVIDENCE_ROOT", "HYPERV_MCP_EVENT_DIRECTORY"],
)
def test_relative_lab_roots_are_rejected_at_startup(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValidationError, match="absolute path"):
        Settings.from_env({"LOCALAPPDATA": str(tmp_path), name: "relative\\directory"})


def test_the_safe_profile_is_the_default_and_enables_nothing(tmp_path: Path) -> None:
    settings = Settings.from_env({"LOCALAPPDATA": str(tmp_path)})
    assert settings.profile == "safe"
    assert settings.allow_power is False
    assert settings.allow_guest is False
    assert settings.allow_media is False


def test_the_lab_profile_enables_every_capability(tmp_path: Path) -> None:
    settings = Settings.from_env({
        "LOCALAPPDATA": str(tmp_path),
        "HYPERV_MCP_PROFILE": "lab",
        "HYPERV_MCP_ALLOWED_VMS": "LAB-Test",
    })
    assert settings.profile == "lab"
    for name in (
        "allow_power", "allow_checkpoints", "allow_guest", "allow_console",
        "allow_screenshots", "allow_debugger", "allow_media",
    ):
        assert getattr(settings, name) is True, name


def test_the_lab_profile_does_not_invent_an_allowlist(tmp_path: Path) -> None:
    settings = Settings.from_env({"LOCALAPPDATA": str(tmp_path), "HYPERV_MCP_PROFILE": "lab"})
    assert settings.allowed_vms == frozenset()


def test_an_explicit_false_holds_one_capability_back_in_the_lab_profile(
    tmp_path: Path,
) -> None:
    settings = Settings.from_env({
        "LOCALAPPDATA": str(tmp_path),
        "HYPERV_MCP_PROFILE": "lab",
        "HYPERV_MCP_ALLOW_MEDIA": "false",
    })
    assert settings.allow_power is True
    assert settings.allow_media is False


def test_an_unknown_profile_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="HYPERV_MCP_PROFILE"):
        Settings.from_env({"LOCALAPPDATA": str(tmp_path), "HYPERV_MCP_PROFILE": "yolo"})


def test_each_profile_starts_from_its_own_toolsets(tmp_path: Path) -> None:
    safe = Settings.from_env({"LOCALAPPDATA": str(tmp_path)})
    lab = Settings.from_env({"LOCALAPPDATA": str(tmp_path), "HYPERV_MCP_PROFILE": "lab"})
    # The everyday surface; the lab cycle, the guest configuration it depends on,
    # and both halves of the debugger.
    assert safe.toolsets == frozenset(
        {"core", "guest", "console", "config", "credentials", "media", "ssh"}
    )
    assert lab.toolsets == frozenset({"core", "guest", "console", "config", "lab", "kd", "debug"})
    assert "lab" not in safe.toolsets
    assert "kd" not in safe.toolsets
    assert "debug" not in safe.toolsets
    # A lab cycle that cannot configure or revert its guest is not a lab profile.
    assert {"config", "debug"} <= lab.toolsets


def test_the_toolset_list_can_be_replaced_entirely(tmp_path: Path) -> None:
    settings = Settings.from_env({
        "LOCALAPPDATA": str(tmp_path),
        "HYPERV_MCP_TOOLSETS": " Core , kd ,, debug ",
    })
    assert settings.toolsets == frozenset({"core", "kd", "debug"})


def test_all_is_shorthand_for_every_toolset(tmp_path: Path) -> None:
    settings = Settings.from_env({"LOCALAPPDATA": str(tmp_path), "HYPERV_MCP_TOOLSETS": "all"})
    assert settings.toolsets == TOOLSETS


def test_an_unknown_toolset_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="unknown toolsets: kernel"):
        Settings.from_env({
            "LOCALAPPDATA": str(tmp_path), "HYPERV_MCP_TOOLSETS": "core,kernel",
        })
