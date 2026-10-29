from __future__ import annotations

from pathlib import Path

import pytest

from hyperv_console_mcp.config import Settings
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
