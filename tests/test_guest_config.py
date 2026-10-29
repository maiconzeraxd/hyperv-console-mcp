"""Guest automation settings: the settings document, the command, and the errors.

The guest is modelled by a fake transport here; nothing in this file touches a VM.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import BackendError
from hyperv_console_mcp.transport import guest_config
from hyperv_console_mcp.transport.ssh import JSON_MARKER

VM = "LAB-Test"
SECRET = "Sup3r-Secret-For-Tests!"


def settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {"allowed_vms": frozenset({VM}), "allow_guest": True}
    values.update(overrides)
    return Settings(**values)


def marker(payload: dict[str, Any]) -> str:
    body = json.dumps(payload)
    return JSON_MARKER + base64.b64encode(body.encode("utf-8")).decode("ascii")


class FakeTransport:
    name = "psdirect"

    def __init__(self, output: str | None = None, *, fail_first_exec: bool = False) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.commands: list[str] = []
        self.output = output if output is not None else marker({"mode": "Apply", "changed": ["autologon"]})
        self.fail_first_exec = fail_first_exec

    def health(self, vm: str) -> dict[str, Any]:
        return {"ok": True}

    def put_file(self, vm: str, local: Path, remote: str, *, overwrite: bool) -> Any:
        content = Path(local).read_bytes()
        self.uploads.append(
            {"remote": remote, "content": content, "overwrite": overwrite, "local": str(local)}
        )
        return SimpleNamespace(path=remote, size=len(content), sha256="0" * 64)

    def exec(self, vm: str, command: str, *, timeout: float, max_output: int) -> Any:
        self.commands.append(command)
        if self.fail_first_exec and len(self.commands) == 1:
            raise BackendError("the guest is unreachable", code="guest_exec_failed")
        return SimpleNamespace(output=self.output)


class FakeService:
    def __init__(self, transport: FakeTransport, settings_value: Settings) -> None:
        self.transport = transport
        self.settings = settings_value

    def _transport(self, vm: str) -> FakeTransport:
        return self.transport


def document(transport: FakeTransport) -> dict[str, Any]:
    """The settings document the host uploaded, decoded."""
    upload = transport.uploads[1]
    return json.loads(upload["content"].decode("utf-8"))


def test_apply_uploads_the_script_then_the_document_and_never_the_secret(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    service = FakeService(transport, settings(tmp_path))

    result = guest_config.apply(
        service, VM,
        autologon={"enabled": True, "username": "mcn1", "domain": ".", "secret": SECRET},
        lock_screen=True, disable_cad=None, power=True, uac=False, windows_update=None,
    )

    assert result["changed"] == ["autologon"]
    # The helper script first, then the settings document.
    assert transport.uploads[0]["remote"] == guest_config.GUEST_SCRIPT
    assert transport.uploads[0]["overwrite"] is True
    assert transport.uploads[1]["remote"].startswith(guest_config.GUEST_SETTINGS_ROOT)
    assert transport.uploads[1]["remote"].endswith(".json")
    assert transport.uploads[1]["overwrite"] is False

    # The password travels in the document, never on the command line.
    body = document(transport)
    assert body["autologon"]["secret"] == SECRET
    assert len(transport.commands) == 1
    assert SECRET not in transport.commands[0]
    assert "-Mode Apply" in transport.commands[0]
    assert guest_config.GUEST_STATE_PATH in transport.commands[0]

    # Tri-state toggles survive: None leaves a setting alone.
    assert body["lock_screen"] is True
    assert body["disable_cad"] is None
    assert body["power"] is True
    assert body["uac"] is False
    assert body["windows_update"] is None


def test_the_local_copy_of_the_document_is_removed(tmp_path: Path) -> None:
    transport = FakeTransport()
    service = FakeService(transport, settings(tmp_path))

    guest_config.apply(
        service, VM, autologon=None,
        lock_screen=None, disable_cad=None, power=None, uac=None, windows_update=None,
    )

    # Only the script upload remains; the document's local directory is gone.
    assert not Path(transport.uploads[1]["local"]).exists()
    assert not Path(transport.uploads[1]["local"]).parent.exists()


def test_a_failed_apply_still_tries_to_remove_the_settings_file_from_the_guest(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(fail_first_exec=True)
    service = FakeService(transport, settings(tmp_path))

    with pytest.raises(BackendError, match="unreachable"):
        guest_config.apply(
            service, VM, autologon=None,
            lock_screen=True, disable_cad=None, power=None, uac=None, windows_update=None,
        )

    guest_path = transport.uploads[1]["remote"]
    assert len(transport.commands) == 2
    assert "Remove-Item" in transport.commands[1]
    assert guest_path in transport.commands[1]


def test_an_error_marker_becomes_a_backend_error(tmp_path: Path) -> None:
    transport = FakeTransport(output="HVMCP_ERROR:Set-LocalUser was denied")
    service = FakeService(transport, settings(tmp_path))

    with pytest.raises(BackendError) as caught:
        guest_config.apply(
            service, VM, autologon=None,
            lock_screen=True, disable_cad=None, power=None, uac=None, windows_update=None,
        )

    assert caught.value.code == "guest_config_failed"
    assert "denied" in str(caught.value)


def test_output_without_a_marker_is_an_error(tmp_path: Path) -> None:
    transport = FakeTransport(output="nothing useful")
    service = FakeService(transport, settings(tmp_path))

    with pytest.raises(BackendError, match="produced no result"):
        guest_config.status(service, VM)


def test_status_sends_no_settings_document(tmp_path: Path) -> None:
    transport = FakeTransport(output=marker({"mode": "Status", "managed_entries": []}))
    service = FakeService(transport, settings(tmp_path))

    result = guest_config.status(service, VM)

    assert result["mode"] == "Status"
    assert [upload["remote"] for upload in transport.uploads] == [guest_config.GUEST_SCRIPT]
    assert "-Mode Status" in transport.commands[0]


def test_revert_asks_the_guest_to_restore_and_checks_health_first(tmp_path: Path) -> None:
    transport = FakeTransport(output=marker({"mode": "Revert", "restored": {"uac": ["X"]}}))
    service = FakeService(transport, settings(tmp_path))

    result = guest_config.revert(service, VM)

    assert result["restored"] == {"uac": ["X"]}
    assert "-Mode Revert" in transport.commands[0]


def test_the_live_view_is_derived_on_the_host_from_raw_guest_facts() -> None:
    """The guest reports registry values, BCD tokens and two memory figures.

    Regression: the guest script decided what they meant — `($value -eq 1)`,
    `-not ($cad -eq 1)`, a dump-mode switch and the pagefile comparison — so the
    same reading existed only in PowerShell and could be checked only on a VM.
    The tool's output is unchanged; where it is computed moved.
    """
    facts = {
        "autologon": {"auto_admin_logon": "1", "default_user_name": "mcn1"},
        "no_lock_screen": 1,
        "disable_cad": 1,
        "consent_prompt_behavior_admin": 0,
        "enable_lua": 1,
        "no_auto_update": 1,
        "no_auto_reboot_with_logged_on_users": 1,
        "screen_save_active": "0",
        "power": {"standby_timeout_ac": 0, "monitor_timeout_ac": 0, "console_lock": 0},
        "interactive_sessions": ["mcn1 console 1 Ativo"],
        "boot": {
            "testsigning": "on", "debug": "on", "crash_dump_enabled": 2,
            "hvci_enabled": 0, "vbs_enabled": 0,
        },
        "dump_capacity": {"memory_bytes": 4 * 1024**3, "pagefile_bytes": 5 * 1024**3},
    }

    live = guest_config.derive_live(facts)

    assert live["lock_screen_disabled"] is True
    assert live["ctrl_alt_del_required"] is False
    assert live["windows_update_disabled"] is True
    assert live["windows_update_auto_reboot_blocked"] is True
    assert live["uac_consent_prompt_for_admins"] == 0
    assert live["screensaver_active"] == "0"
    assert live["driver_ready"] == {
        "test_signing": True,
        "kernel_debug": True,
        "crash_dump_enabled": 2,
        "crash_dump_mode": "kernel",
        "hvci_enabled": False,
        "vbs_enabled": False,
        "dump_support": {
            "memory_bytes": 4 * 1024**3,
            "pagefile_bytes": 5 * 1024**3,
            "sufficient_for_complete_dump": True,
        },
    }


def test_a_missing_value_stays_missing_and_a_small_pagefile_is_not_enough() -> None:
    """Absent is not zero, and only the pagefile decides a complete dump.

    A guest with no `DisableCAD` does require Ctrl+Alt+Del, and one with no
    `CrashDumpEnabled` has no dump configured — those two are Windows defaults,
    not invented values. Everything else reads back as null.
    """
    live = guest_config.derive_live({
        "boot": {"testsigning": "off", "crash_dump_enabled": None},
        "dump_capacity": {"memory_bytes": 8 * 1024**3, "pagefile_bytes": 1 * 1024**3},
    })

    assert live["lock_screen_disabled"] is False
    assert live["ctrl_alt_del_required"] is True
    assert live["uac_consent_prompt_for_admins"] is None
    assert live["screensaver_active"] is None
    assert live["driver_ready"]["test_signing"] is False
    assert live["driver_ready"]["kernel_debug"] is False
    assert live["driver_ready"]["crash_dump_enabled"] is None
    assert live["driver_ready"]["crash_dump_mode"] == "none"
    assert live["driver_ready"]["hvci_enabled"] is False
    assert live["driver_ready"]["dump_support"]["sufficient_for_complete_dump"] is False


def test_a_registry_value_of_one_reads_as_on_only_when_it_is_a_number() -> None:
    """`"1"` and `True` are not a DWORD of one; only the integer is."""
    live = guest_config.derive_live({"no_lock_screen": "1", "disable_cad": True})
    assert live["lock_screen_disabled"] is False
    assert live["ctrl_alt_del_required"] is True


def test_the_guest_script_is_the_one_shipped_with_the_package() -> None:
    source = Path(guest_config.__file__).with_name("guest_config.ps1")
    text = source.read_text(encoding="utf-8")
    # The state file the roadmap asks for, and the LSA route for the password.
    assert guest_config.GUEST_STATE_PATH == r"C:\DriverLab\State\guest-config.json"
    assert "LsaStorePrivateData" in text
    assert "DevicePasswordLessBuildVersion" in text
    assert "ConsentPromptBehaviorAdmin" in text
    assert "NoLockScreen" in text
    assert "DisableCAD" in text
    # Driver-lab entries: boot values go through bcdedit, the rest through the
    # same recorded registry path so guest_config_revert can undo them.
    assert "bcdedit.exe /set" in text
    assert "'testsigning'" in text
    assert "CrashDumpEnabled" in text
    assert "HypervisorEnforcedCodeIntegrity" in text
    assert "EnableVirtualizationBasedSecurity" in text


def test_lab_apply_sends_only_the_driver_entries(tmp_path: Path) -> None:
    transport = FakeTransport(output=marker({"mode": "Apply", "changed": ["testsigning:on"]}))
    service = FakeService(transport, settings(tmp_path))

    result = guest_config.lab_apply(
        service, VM, testsigning=True, kernel_debug=None, crash_dump="kernel", hvci=False
    )

    assert result["changed"] == ["testsigning:on"]
    assert document(transport) == {
        "testsigning": True,
        "kernel_debug": None,
        "crash_dump": "kernel",
        "hvci": False,
    }
    # The autologon section is absent, so no password is shipped with a lab change.
    assert "autologon" not in document(transport)
    assert SECRET not in transport.commands[0]


STATE_WITH_ONE_ENTRY = {
    "version": 1,
    "updated_at": "2026-10-29T10:41:00.0000000Z",
    "entries": {
        "lock_screen": {
            "values": [
                {
                    "path": r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization",
                    "name": "NoLockScreen",
                    "existed": False,
                    "kind": None,
                    "previous": None,
                }
            ],
            "lsa": [],
            "power": [],
            "bcd": [],
            "applied": {"no_lock_screen": True},
        }
    },
}


WINDOWS_POWERSHELL_ONLY = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell") is None,
    reason="needs Windows PowerShell 5.1 to run the guest script",
)


def run_guest_probe(tmp_path: Path, lines: list[str]) -> Any:
    """Dot-source the guest script in a fresh powershell.exe and run ``lines``.

    The script only runs its mode when it is executed, so dot-sourcing it loads
    the functions against a temporary state file and touches no guest.
    """
    script = Path(guest_config.__file__).with_name("guest_config.ps1")
    probe = tmp_path / "probe.ps1"
    probe.write_text(
        "\n".join(["$ErrorActionPreference = 'Stop'", f". '{script}'", *lines]),
        encoding="utf-8-sig",
    )
    completed = subprocess.run(
        [shutil.which("powershell") or "powershell", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-File", str(probe)],
        capture_output=True, text=True, timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@WINDOWS_POWERSHELL_ONLY
def test_a_recorded_state_can_be_read_and_extended_again(tmp_path: Path) -> None:
    """A second Apply reads the state file the first one wrote.

    Regression: ``ConvertFrom-Json`` hands back PSCustomObjects, and
    ``Get-EntryRecord`` extends a record with ``$record[$field] = @()``, which
    throws "cannot index into a PSObject". The first Apply worked because no
    state file existed yet; every Apply after it failed on the live guest.
    """
    state_file = tmp_path / "guest-config.json"
    state_file.write_text(json.dumps(STATE_WITH_ONE_ENTRY), encoding="utf-8")

    payload = run_guest_probe(tmp_path, [
        f"$script:StateFile = '{state_file}'",
        "$state = Get-ConfigState",
        "$record = Get-EntryRecord -State $state -Entry 'lock_screen'",
        "$record.values += @{ path = 'HKCU:\\Control Panel\\Desktop'; name = 'ScreenSaveActive'"
        "; existed = $true; kind = 'String'; previous = '1' }",
        "$record.applied['no_lock_screen'] = $true",
        "@{ entries = @($state.entries.Keys); values = @($record.values).Count;"
        " applied = $record.applied['no_lock_screen'] } | ConvertTo-Json -Compress",
    ])

    assert payload["entries"] == ["lock_screen"]
    assert payload["values"] == 2
    assert payload["applied"] is True


@WINDOWS_POWERSHELL_ONLY
def test_bcd_values_are_read_from_tokens_that_are_never_translated(tmp_path: Path) -> None:
    """Regression: the reader compared the value against 'on', which bcdedit never prints.

    bcdedit translates the labels around each boot entry, not the entries: a
    pt-BR guest prints the same ``testsigning             Yes`` an English one
    does. Comparing that token against 'on' was false on every guest, so Status
    reported test signing and kernel debugging as off while both were on, and
    Revert wrote "Yes" back as if it were a value bcdedit understands.
    """
    fixture = Path(__file__).with_name("fixtures") / "bcd-current-pt-br.txt"

    payload = run_guest_probe(tmp_path, [
        f"$text = Get-Content -LiteralPath '{fixture}' -Raw -Encoding UTF8",
        "$r = @{}",
        "foreach ($n in @('testsigning', 'debug', 'nx', 'bootux')) {",
        "    $r[$n] = ConvertFrom-BcdEnum -Text $text -Name $n",
        "}",
        "$r | ConvertTo-Json -Depth 4 -Compress",
    ])

    assert payload["testsigning"] == {"existed": True, "value": "on"}
    assert payload["debug"] == {"existed": True, "value": "on"}
    # A value that is not a boolean is passed through untouched.
    assert payload["nx"] == {"existed": True, "value": "OptIn"}
    # An entry bcdedit does not list is absent, not off.
    assert payload["bootux"] == {"existed": False, "value": None}


@WINDOWS_POWERSHELL_ONLY
def test_the_power_values_are_read_without_parsing_localized_output(tmp_path: Path) -> None:
    """powercfg prints its labels in the display language.

    Regression: the reader matched the English label "Current AC Power Setting
    Index", so on a pt-BR guest every power value came back null even though the
    settings had been applied. The reader now goes through the CIM power
    provider, which is language-neutral.
    """
    payload = run_guest_probe(tmp_path, [
        "$values = @{}",
        "foreach ($name in @('STANDBYIDLE', 'VIDEOIDLE', 'CONSOLELOCK')) {",
        "    $value = Get-PowerSetting -Setting $name",
        "    $values[$name] = @{ type = $(if ($null -eq $value) { 'null' }"
        " else { $value.GetType().Name }); value = $value }",
        "}",
        "$values | ConvertTo-Json -Compress",
    ])

    assert set(payload) == {"STANDBYIDLE", "VIDEOIDLE", "CONSOLELOCK"}
    for name, reading in payload.items():
        assert reading["type"] == "Int32", f"{name}: {reading}"
        assert reading["value"] >= 0
