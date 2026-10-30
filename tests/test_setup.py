"""The install routine's decisions, exercised on hosts a developer never has.

Windows Home, a feature installed but switched off, a machine with no MCP client
on it, a service that is not listening: the routine answers each of them with a
sentence and an action, and the answers are what these tests pin down. Nothing
here touches the registry, the shell or Hyper-V — the routine asks through
``probe``, ``exists`` and ``environ`` so the answers can be supplied.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp import install, setup
from hyperv_console_mcp.errors import HyperVError


class FakeService:
    """A service that answers from its own bus, with the failures selectable."""

    def __init__(
        self,
        *,
        vms: int = 1,
        snapshot_error: Exception | None = None,
        host_error: Exception | None = None,
    ) -> None:
        self.vms = vms
        self.snapshot_error = snapshot_error
        self.host_error = host_error

    def event_snapshot(self) -> dict[str, Any]:
        if self.snapshot_error:
            raise self.snapshot_error
        return {"events": []}

    def host_status(self) -> dict[str, Any]:
        if self.host_error:
            raise self.host_error
        return {"hypervisor_present": True}

    def list_vms(self) -> dict[str, Any]:
        return {"vms": [{"name": f"VM-{index}"} for index in range(self.vms)]}


# -- what the machine is -------------------------------------------------

def test_home_is_refused_with_an_edition_that_works() -> None:
    plan = setup.assess(setup.Host(edition="Core", product="Windows 11 Home", hyperv="enabled"))

    assert plan["action"] == "stop"
    assert plan["supports_hyperv"] is False
    # The sentence names the machine and the way out, not the feature's id.
    assert "Windows 11 Home" in plan["message"]
    assert "Pro" in plan["message"] and "Enterprise" in plan["message"]


def test_every_server_edition_can_host_hyperv() -> None:
    for edition in ("Professional", "Enterprise", "Education", "ServerStandard", "ServerDatacenter"):
        assert setup.supports_hyperv(edition), edition
    for edition in ("Core", "Home", "HomeSingleLanguage", "Starter"):
        assert not setup.supports_hyperv(edition), edition


def test_an_edition_nobody_recognises_is_not_declared_unsupported() -> None:
    """Only the Home family is refused; a name from a future SKU is not a guess."""
    assert setup.supports_hyperv("") is True
    assert setup.supports_hyperv("Windows 12 Pro") is True


def test_a_feature_that_is_installed_and_off_offers_the_command_that_turns_it_on() -> None:
    plan = setup.assess(setup.Host(edition="Professional", hyperv="disabled"))

    assert plan["action"] == "enable-hyperv"
    assert plan["command"] == setup.ENABLE_FEATURE
    assert "restart" in plan["message"]


def test_a_feature_that_is_not_installed_says_where_to_add_it() -> None:
    plan = setup.assess(setup.Host(edition="Professional", hyperv="absent"))

    assert plan["action"] == "stop"
    assert "Turn Windows features on or off" in plan["message"]


def test_a_machine_that_will_not_answer_does_not_block_the_install() -> None:
    """The self-test decides whether Hyper-V works; a blind pre-check must not."""
    plan = setup.assess(setup.Host(edition="Professional", hyperv="unknown"))

    assert plan["action"] == "proceed"
    assert "check at the end" in plan["message"]


def test_a_ready_host_proceeds_with_its_name_in_the_message() -> None:
    plan = setup.assess(setup.Host(edition="Professional", product="Windows 11 Pro", hyperv="enabled"))

    assert plan["action"] == "proceed"
    assert plan["command"] is None
    assert "Windows 11 Pro" in plan["message"]


def test_a_machine_that_is_not_windows_is_told_so() -> None:
    plan = setup.assess(setup.Host())

    assert plan["action"] == "stop"
    assert "Windows only" in plan["message"]


def test_the_probe_turns_the_install_state_into_a_word(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup.os, "name", "nt")
    monkeypatch.setattr(setup, "_edition", lambda: ("Professional", "Windows 11 Pro"))

    for state, word in (("1", "enabled"), ("2", "disabled"), ("3", "absent"), ("", "unknown")):
        host = setup.read_host(lambda _script, answer=state: answer)
        assert host.hyperv == word, state
        assert host.edition == "Professional"


def test_a_non_windows_host_is_not_asked_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup.os, "name", "posix")

    def probe(_script: str) -> str:
        raise AssertionError("a non-Windows host must not be probed")

    assert setup.read_host(probe) == setup.Host()


# -- the clients ---------------------------------------------------------

def test_a_client_is_found_by_its_own_directory_not_its_config_file(tmp_path: Path) -> None:
    """A fresh Claude Desktop has the folder and no file yet; that is the case to catch."""
    (tmp_path / "appdata" / "Claude").mkdir(parents=True)
    (tmp_path / ".codex").mkdir()
    environ = {"USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path / "appdata")}

    assert setup.detect_clients(environ) == ["claude", "codex"]


def test_a_machine_with_no_client_gets_a_snippet_instead_of_silence(
    tmp_path: Path,
) -> None:
    environ = {"USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path / "appdata")}

    result = setup.configure_clients(environ=environ)

    assert result["clients"] == []
    assert result["written"] == []
    snippet = result["snippet"]
    assert snippet["entry"]["args"] == ["-m", "hyperv_console_mcp.server"]
    # The operator is told where each client keeps the file, not left to search.
    assert set(snippet["paste_into"]) == set(install.CLIENTS)
    assert snippet["paste_into"]["cursor"].endswith("mcp.json")


def test_a_detected_client_gets_its_config_written(tmp_path: Path) -> None:
    (tmp_path / ".cursor").mkdir()
    environ = {"USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path / "appdata")}

    result = setup.configure_clients(environ=environ)

    assert result["clients"] == ["cursor"]
    assert result["snippet"] is None
    document = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert document["mcpServers"]["hyperv"]["command"] == sys.executable


# -- the self-test -------------------------------------------------------

def test_a_service_that_is_not_listening_says_which_step_failed() -> None:
    result = setup.self_test(FakeService(snapshot_error=HyperVError("the pipe closed")))

    assert result["ok"] is False
    assert result["step"] == "service"
    assert "hyperv-service" in result["message"]


def test_a_running_service_without_hyperv_points_at_the_group() -> None:
    result = setup.self_test(FakeService(host_error=HyperVError("access denied")))

    assert result["ok"] is False
    assert result["step"] == "hyper-v"
    assert "Hyper-V Administrators" in result["message"]


def test_a_host_with_no_vms_is_ready_but_says_what_to_do_next() -> None:
    result = setup.self_test(FakeService(vms=0))

    assert result["ok"] is True
    assert result["vms"] == 0
    assert "no virtual machines" in result["message"]


def test_a_host_with_vms_says_which_shortcut_to_open() -> None:
    result = setup.self_test(FakeService(vms=2))

    assert result == {
        "ok": True, "step": "ready", "vms": 2,
        "message": (
            "Everything is ready. Open 'Hyper-V Console' from the Start Menu; the first "
            "run asks which virtual machine to control."
        ),
    }


# -- the routine ---------------------------------------------------------

def test_the_routine_stops_on_home_before_touching_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup.os, "name", "nt")
    monkeypatch.setattr(setup, "_edition", lambda: ("Core", "Windows 11 Home"))
    written: list[str] = []
    monkeypatch.setattr(install, "install", lambda client, **_kwargs: written.append(client))

    result = setup.first_run(
        environ={"USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path / "appdata")},
        probe=lambda _script: "1",
    )

    assert result["ok"] is False
    assert result["stopped"] == "host"
    assert written == []


def test_the_routine_stops_for_the_restart_rather_than_taking_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabling Hyper-V needs a reboot; an installer that reboots by itself is a surprise."""
    monkeypatch.setattr(setup.os, "name", "nt")
    monkeypatch.setattr(setup, "_edition", lambda: ("Professional", "Windows 11 Pro"))

    result = setup.first_run(
        environ={"USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path / "appdata")},
        probe=lambda _script: "2",
    )

    assert result["stopped"] == "hyper-v"
    assert result["command"] == setup.ENABLE_FEATURE
    assert "steps" in result and "clients" not in result["steps"]


def test_a_second_run_repairs_and_does_not_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup.os, "name", "nt")
    monkeypatch.setattr(setup, "_edition", lambda: ("Professional", "Windows 11 Pro"))
    environ = {"USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path / "appdata")}
    (tmp_path / ".cursor").mkdir()
    service = FakeService()

    first = setup.first_run(environ=environ, service=service, probe=lambda _s: "1", shortcut=False)
    second = setup.first_run(environ=environ, service=service, probe=lambda _s: "1", shortcut=False)

    assert first["ok"] is True and second["ok"] is True
    assert first["steps"]["clients"]["written"][0]["action"] == "created"
    assert second["steps"]["clients"]["written"][0]["action"] == "unchanged"


def test_the_routine_reports_a_service_that_did_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup.os, "name", "nt")
    monkeypatch.setattr(setup, "_edition", lambda: ("Professional", "Windows 11 Pro"))

    result = setup.first_run(
        environ={"USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path / "appdata")},
        service=FakeService(snapshot_error=HyperVError("no host")),
        probe=lambda _s: "1", shortcut=False,
    )

    assert result["ok"] is False
    assert result["steps"]["self_test"]["step"] == "service"


# -- uninstall -----------------------------------------------------------

def test_uninstall_takes_our_entry_out_and_leaves_the_other_servers(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".cursor" / "mcp.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"mcpServers": {
        "other": {"command": "npx", "args": ["x"]},
        "hyperv": {"command": "python", "args": ["-m", "hyperv_console_mcp.server"]},
    }}), encoding="utf-8")

    result = setup.remove_client_entry("cursor", environ={"USERPROFILE": str(tmp_path)})

    assert result["action"] == "removed"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert list(document["mcpServers"]) == ["other"]
    assert Path(result["backup"]).is_file()


def test_uninstall_leaves_a_file_it_cannot_parse_exactly_as_it_was(tmp_path: Path) -> None:
    path = tmp_path / ".cursor" / "mcp.json"
    path.parent.mkdir()
    path.write_text("{not json", encoding="utf-8")

    result = setup.remove_client_entry("cursor", environ={"USERPROFILE": str(tmp_path)})

    assert result["action"] == "refused"
    assert path.read_text(encoding="utf-8") == "{not json"


def test_uninstall_takes_only_our_toml_section(tmp_path: Path) -> None:
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text(
        'model = "gpt-5"\n\n[mcp_servers.hyperv]\ncommand = "python"\n\n'
        '[mcp_servers.hyperv.env]\nHYPERV_MCP_PROFILE = "lab"\n\n'
        '[mcp_servers.other]\ncommand = "x"\n',
        encoding="utf-8",
    )

    result = setup.remove_client_entry("codex", environ={"USERPROFILE": str(tmp_path)})

    text = path.read_text(encoding="utf-8")
    assert result["action"] == "removed"
    assert 'model = "gpt-5"' in text
    assert "[mcp_servers.other]" in text
    assert "mcp_servers.hyperv" not in text


def test_uninstall_removes_the_shortcut_and_keeps_the_operator_s_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environ = {"USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path / "appdata"),
               "LOCALAPPDATA": str(tmp_path / "local")}
    shortcut = install.shortcut_path(environ)
    shortcut.parent.mkdir(parents=True)
    shortcut.write_text("", encoding="utf-8")
    (tmp_path / "local" / "HyperVConsoleMCP" / "evidence").mkdir(parents=True)

    result = setup.uninstall(environ=environ)

    assert result["shortcut"]["action"] == "removed"
    assert not shortcut.exists()
    # Removing a folder the operator may still need is not an uninstaller's call.
    assert result["data"]["action"] == "kept"
    assert (tmp_path / "local" / "HyperVConsoleMCP" / "evidence").is_dir()
    assert result["credentials"]["action"] == "kept"
    assert "--remove-data" in result["message"]


def test_uninstall_removes_credentials_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environ = {"USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path / "appdata"),
               "LOCALAPPDATA": str(tmp_path / "local")}
    access = tmp_path / "access.json"
    environ["HYPERV_MCP_ACCESS_FILE"] = str(access)
    from hyperv_console_mcp.access import AccessStore

    AccessStore(access).set("NORTH-Test", "lab")
    deleted: list[str] = []

    class FakeStore:
        def __init__(self, *_args: Any) -> None:
            pass

        def delete(self, vm: str) -> bool:
            deleted.append(vm)
            return True

    monkeypatch.setattr(setup, "CredentialStore", FakeStore)
    monkeypatch.setattr(setup, "SecretStore", FakeStore)

    result = setup.uninstall(environ=environ, credentials=True)

    assert result["credentials"]["action"] == "removed"
    # The spelling the access file records, because the vault's target is built
    # from it: deleting another case would leave the secret behind.
    assert deleted == ["NORTH-Test", "NORTH-Test"]


def test_uninstall_asks_about_the_folders_before_deleting_them(tmp_path: Path) -> None:
    environ = {"USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path / "appdata"),
               "LOCALAPPDATA": str(tmp_path / "local")}
    (tmp_path / "local" / "HyperVConsoleMCP" / "events").mkdir(parents=True)

    result = setup.uninstall(environ=environ, data=True)

    assert result["data"]["action"] == "removed"
    assert not (tmp_path / "local" / "HyperVConsoleMCP").exists()


# -- the command ---------------------------------------------------------

def test_the_check_reports_without_changing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(setup.os, "name", "nt")
    monkeypatch.setattr(setup, "_edition", lambda: ("Core", "Windows 11 Home"))
    monkeypatch.setattr(setup, "_run", lambda _script: "1")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert setup.main(["--check"]) == 1

    printed = json.loads(capsys.readouterr().out)
    assert printed["action"] == "stop"
    assert not (tmp_path / ".cursor").exists()


# -- the PowerShell wrapper ----------------------------------------------

def wrapper() -> str:
    return (Path(__file__).parents[1] / "scripts" / "install.ps1").read_text(encoding="utf-8")


def test_the_wrapper_only_passes_flags_the_routine_accepts() -> None:
    """A wrapper that calls a flag nobody implemented fails at the operator's desk."""
    import re

    known = {
        option
        for action in setup._parser()._actions
        for option in action.option_strings
    }
    used = set(re.findall(r"'(--[a-z-]+)'", wrapper()))

    assert used, "the wrapper should call the routine"
    assert used <= known, used - known


def test_the_wrapper_enables_the_feature_the_routine_names() -> None:
    assert setup.ENABLE_FEATURE in wrapper()
