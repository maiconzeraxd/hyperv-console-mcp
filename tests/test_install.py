"""``hyperv-install`` writes one entry into a client's own config and nothing else."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyperv_console_mcp import install
from hyperv_console_mcp.errors import ValidationError


def test_each_client_keeps_its_config_where_it_already_is(tmp_path: Path) -> None:
    environ = {"USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path / "appdata")}
    assert install.client_config("claude", environ) == (
        tmp_path / "appdata" / "Claude" / "claude_desktop_config.json"
    )
    assert install.client_config("cursor", environ) == tmp_path / ".cursor" / "mcp.json"
    assert install.client_config("codex", environ) == tmp_path / ".codex" / "config.toml"
    with pytest.raises(ValidationError, match="Unknown client"):
        install.client_config("vim", environ)


def test_the_entry_is_the_interpreter_that_installed_it() -> None:
    entry = install.server_entry()
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-m", "hyperv_console_mcp.server"]
    assert "env" not in entry


def test_uvx_and_a_profile_are_written_into_the_entry() -> None:
    entry = install.server_entry(uvx=True, profile="lab")
    assert entry == {"command": "uvx", "args": ["hyperv-console-mcp"], "env": {"HYPERV_MCP_PROFILE": "lab"}}


def test_a_new_json_config_is_created_with_only_our_entry(tmp_path: Path) -> None:
    path = tmp_path / "claude_desktop_config.json"
    result = install.install_json("claude", path, install.server_entry())
    assert result["action"] == "created"
    assert result["backup"] is None
    document = json.loads(path.read_text(encoding="utf-8"))
    assert list(document["mcpServers"]) == ["hyperv"]
    assert document["mcpServers"]["hyperv"]["args"] == ["-m", "hyperv_console_mcp.server"]


def test_another_client_s_servers_survive_the_merge(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "npx", "args": ["x"]}}, "theme": "dark"}),
        encoding="utf-8",
    )
    result = install.install_json("cursor", path, install.server_entry())
    document = json.loads(path.read_text(encoding="utf-8"))
    assert result["action"] == "updated"
    assert document["theme"] == "dark"
    assert document["mcpServers"]["other"] == {"command": "npx", "args": ["x"]}
    assert document["mcpServers"]["hyperv"]["command"] == sys.executable
    assert Path(result["backup"]).is_file()


def test_running_it_twice_changes_nothing_the_second_time(tmp_path: Path) -> None:
    """An install that rewrites an identical file would pile up backups."""
    path = tmp_path / "mcp.json"
    install.install_json("cursor", path, install.server_entry())
    before = path.read_text(encoding="utf-8")
    result = install.install_json("cursor", path, install.server_entry())
    assert result["action"] == "unchanged"
    assert "backup" not in result
    assert path.read_text(encoding="utf-8") == before


def test_a_config_we_do_not_understand_is_refused_not_replaced(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationError, match="not valid JSON"):
        install.install_json("cursor", path, install.server_entry())
    assert path.read_text(encoding="utf-8") == "{not json"


def test_an_mcp_servers_that_is_not_a_map_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": ["hyperv"]}), encoding="utf-8")
    with pytest.raises(ValidationError, match="not an object"):
        install.install_json("cursor", path, install.server_entry())
    assert json.loads(path.read_text(encoding="utf-8")) == {"mcpServers": ["hyperv"]}


def test_the_toml_section_is_appended_after_what_is_there(tmp_path: Path) -> None:
    """Codex's file is somebody's config; appending is all we may do to it."""
    path = tmp_path / "config.toml"
    path.write_text('model = "gpt-5"\n\n# keep this comment\n', encoding="utf-8")
    result = install.install_toml("codex", path, install.server_entry(profile="lab"))
    text = path.read_text(encoding="utf-8")
    assert result["action"] == "updated"
    assert text.startswith('model = "gpt-5"\n\n# keep this comment\n')
    assert "[mcp_servers.hyperv]" in text
    assert "[mcp_servers.hyperv.env]" in text
    assert 'HYPERV_MCP_PROFILE = "lab"' in text


def test_an_existing_toml_section_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[mcp_servers.hyperv]\ncommand = \"python\"\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    result = install.install_toml("codex", path, install.server_entry())
    assert result["action"] == "unchanged"
    assert path.read_text(encoding="utf-8") == before


def test_a_missing_toml_file_is_created_whole(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.toml"
    result = install.install_toml("codex", path, install.server_entry())
    assert result["action"] == "created"
    assert path.read_text(encoding="utf-8").startswith("[mcp_servers.hyperv]")


def test_the_shortcut_lands_in_the_start_menu(tmp_path: Path) -> None:
    path = install.shortcut_path({"APPDATA": str(tmp_path)})
    assert path.name == "Hyper-V Console MCP.lnk"
    assert "Start Menu" in str(path)


def test_a_shortcut_outside_windows_is_skipped_rather_than_faked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install, "os", SimpleNamespace(name="posix"))
    result = install.create_shortcut(tmp_path / "x.lnk")
    assert result["action"] == "skipped"
    assert not (tmp_path / "x.lnk").exists()


def test_the_command_writes_the_client_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(sys, "argv", ["hyperv-install", "--client", "cursor", "--profile", "lab"])
    install.main()
    document = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert document["mcpServers"]["hyperv"]["env"] == {"HYPERV_MCP_PROFILE": "lab"}


def test_asking_for_nothing_is_an_error_not_a_silent_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["hyperv-install"])
    with pytest.raises(SystemExit) as caught:
        install.main()
    assert caught.value.code == 2
