"""Test kernel-driver services: the arguments, the guest program, and the codes.

The guest is a fake transport here; the PowerShell tests dot-source the guest
script against a temporary state file and read the real Service Control Manager,
which needs no elevation for a status query.
"""

from __future__ import annotations

import base64
import contextlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import BackendError, PolicyError, ValidationError
from hyperv_console_mcp.service.lab import LabService
from hyperv_console_mcp.transport import guest_driver_service
from hyperv_console_mcp.transport.ssh import ERROR_MARKER, JSON_MARKER

VM = "LAB-Test"
IMAGE = r"C:\DriverLab\Packages\abc\mydriver.sys"


def marker(payload: dict[str, Any]) -> str:
    body = json.dumps(payload)
    return JSON_MARKER + base64.b64encode(body.encode("utf-8")).decode("ascii")


class FakeTransport:
    name = "psdirect"

    def __init__(self, output: str | None = None) -> None:
        self.commands: list[str] = []
        self.uploads: list[str] = []
        self.output = output if output is not None else marker({"mode": "Status", "services": []})

    def health(self, vm: str) -> dict[str, Any]:
        return {"ok": True}

    def put_file(self, vm: str, local: Path, remote: str, *, overwrite: bool) -> Any:
        self.uploads.append(remote)
        return SimpleNamespace(path=remote, size=0, sha256="0" * 64)

    def exec(self, vm: str, command: str, *, timeout: float, max_output: int) -> Any:
        self.commands.append(command)
        return SimpleNamespace(output=self.output, success=True, exit_code=0)


def service_with(transport: FakeTransport) -> SimpleNamespace:
    settings = Settings(allowed_vms=frozenset({VM}), allow_guest=True)
    return SimpleNamespace(_transport=lambda vm: transport, settings=settings)


def run_guest_probe(tmp_path: Path, lines: list[str]) -> Any:
    """Dot-source the guest script in a fresh powershell.exe and run ``lines``."""
    script = guest_driver_service.GUEST_SCRIPT
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


WINDOWS_POWERSHELL_ONLY = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell") is None,
    reason="needs Windows PowerShell 5.1 to run the guest script",
)


def test_the_script_is_shipped_next_to_this_module() -> None:
    assert guest_driver_service.GUEST_SCRIPT.is_file()
    assert guest_driver_service.GUEST_SCRIPT.name == "guest_driver_service.ps1"


def test_install_names_the_image_and_the_start_type() -> None:
    transport = FakeTransport()
    guest_driver_service.apply(
        service_with(transport), VM,
        **guest_driver_service.request("install", "hvmcp_test", IMAGE, "demand"),
    )
    command = transport.commands[-1]
    assert "-Mode Install" in command
    assert "-ServiceName 'hvmcp_test'" in command
    assert f"-ImagePath '{IMAGE}'" in command
    assert "-StartType 'demand'" in command
    assert guest_driver_service.GUEST_STATE_PATH in command
    assert transport.uploads[-1].endswith("guest_driver_service.ps1")


def test_status_lists_what_was_installed_without_a_name() -> None:
    transport = FakeTransport()
    guest_driver_service.apply(service_with(transport), VM, **guest_driver_service.request("status", ""))
    command = transport.commands[-1]
    assert "-Mode Status" in command
    assert "-ServiceName" not in command


def test_a_driver_outside_the_lab_root_is_refused() -> None:
    """Only an image the operator already staged into the lab can be installed."""
    for bad in [
        r"C:\Windows\System32\drivers\evil.sys",
        r"C:\DriverLab\..\evil.sys",
        r"C:\DriverLab",
        r"mydriver.sys",
        r"C:\DriverLab\mydriver.exe",
        "",
    ]:
        with pytest.raises(ValidationError):
            guest_driver_service.request("install", "hvmcp_test", bad)


def test_a_service_name_is_a_name_and_never_a_path() -> None:
    assert guest_driver_service.normalise_service_name(" hvmcp_test ") == "hvmcp_test"
    assert guest_driver_service.normalise_service_name("", allow_empty=True) == ""
    for bad in [r"..\hvmcp", "a b", "..", "", "hvmcp/test", r"C:\hvmcp"]:
        with pytest.raises(ValidationError):
            guest_driver_service.normalise_service_name(bad)


def test_the_start_type_is_one_of_four() -> None:
    assert guest_driver_service.normalise_start_type("DEMAND") == "demand"
    assert guest_driver_service.normalise_start_type(None) == "demand"
    with pytest.raises(ValidationError):
        guest_driver_service.normalise_start_type("manual")


def test_only_install_takes_an_image_and_a_start_type() -> None:
    """A value that would be silently ignored is refused instead."""
    with pytest.raises(ValidationError):
        guest_driver_service.request("start", "hvmcp_test", IMAGE)
    with pytest.raises(ValidationError):
        guest_driver_service.request("stop", "hvmcp_test", "", "boot")


def test_an_error_marker_becomes_a_backend_error() -> None:
    transport = FakeTransport(output=ERROR_MARKER + "OpenSCManager failed with Win32 error 5")
    with pytest.raises(BackendError) as caught:
        guest_driver_service.apply(service_with(transport), VM, **guest_driver_service.request("start", "x"))
    assert caught.value.code == "guest_driver_service_failed"


def test_an_unsigned_driver_is_reported_and_not_worked_around() -> None:
    """A refused load is a fact to report, never a step to get around."""
    result = guest_driver_service.derive({
        "action_ok": False, "win32_error": 577,
        "service": {"exists": True, "state_code": 1, "win32_exit_code": 0, "service_exit_code": 0},
    })
    assert result["ok"] is False
    assert result["win32_error_name"] == "invalid_image_hash"
    assert result["ntstatus"] == "0xC0000428"


def test_the_drivers_own_ntstatus_is_reported_when_the_scm_has_it() -> None:
    result = guest_driver_service.derive({
        "action_ok": False, "win32_error": 1066,
        "service": {
            "exists": True, "state_code": 1,
            "win32_exit_code": guest_driver_service.SERVICE_SPECIFIC_ERROR,
            "service_exit_code": 0xC0000001,
        },
    })
    assert result["ntstatus"] == "0xC0000001"
    assert result["win32_error_name"] == "service_specific_error"
    assert result["service"]["state"] == "stopped"


def test_a_successful_start_has_no_error_to_name() -> None:
    result = guest_driver_service.derive({
        "action_ok": True, "win32_error": 0,
        "service": {"exists": True, "state_code": 4, "win32_exit_code": 0, "service_exit_code": 0},
    })
    assert result["ok"] is True
    assert result["win32_error_name"] is None
    assert result["ntstatus"] is None
    assert result["service"]["state"] == "running"


def test_a_read_has_no_verdict() -> None:
    result = guest_driver_service.derive({"mode": "Status", "services": []})
    assert result["ok"] is None


class FakeLabService:
    """Only the three helpers ``LabService.lab_driver_service`` uses."""

    def __init__(self, transport: FakeTransport) -> None:
        self.transport = transport
        self.settings = Settings(allowed_vms=frozenset({VM}), allow_guest=True)
        self.approvals: list[dict[str, Any]] = []

    def _transport(self, vm: str) -> FakeTransport:
        return self.transport

    def _guest_vm(self, name: str) -> str:
        return name

    @staticmethod
    def _confirm(actual: str, expected: str, label: str) -> None:
        if actual != expected:
            raise PolicyError(f"{label} must exactly match {expected!r}.")

    def _approve(self, operation: str, vm_name: str, summary: dict[str, Any] | None = None):
        self.approvals.append({"operation": operation, "vm": vm_name, "summary": summary})
        return contextlib.nullcontext()


def test_reading_the_state_needs_no_confirmation() -> None:
    """A status call is a read; asking for confirm_vm on it would teach nothing."""
    transport = FakeTransport()
    service = FakeLabService(transport)
    LabService.lab_driver_service(service, VM, "status", "", "")
    assert service.approvals == []
    assert "-Mode Status" in transport.commands[-1]


def test_a_change_is_confirmed_and_approved_with_what_will_run() -> None:
    transport = FakeTransport()
    service = FakeLabService(transport)
    with pytest.raises(PolicyError):
        LabService.lab_driver_service(service, VM, "install", "hvmcp_test", "other-vm", IMAGE)
    assert transport.commands == []
    LabService.lab_driver_service(service, VM, "install", "hvmcp_test", VM, IMAGE, "demand")
    assert len(service.approvals) == 1
    summary = service.approvals[0]["summary"]
    assert summary["action"] == "install"
    assert summary["image_path"] == IMAGE
    # The approval card says, in the record, that no signature boundary moves.
    assert summary["bypasses_signature_enforcement"] is False


def test_a_state_number_becomes_a_name() -> None:
    """The guest's number is the fact; the name is decided where it can be tested."""
    result = guest_driver_service.derive({
        "services": [{"name": "x", "live": {"exists": True, "state_code": 99}}],
    })
    assert result["services"][0]["live"]["state"] == "unknown_99"


@WINDOWS_POWERSHELL_ONLY
def test_a_recorded_service_is_read_back(tmp_path: Path) -> None:
    state_file = tmp_path / "driver-services.json"
    payload = run_guest_probe(tmp_path, [
        f"$script:StateFile = '{state_file}'",
        "$state = @{ version = 1; services = @{ hvmcp_test = @{ image_path ="
        " 'C:\\DriverLab\\Packages\\abc\\mydriver.sys'; start_type = 'demand';"
        " installed_utc = '2026-10-29T00:00:00Z' } } }",
        "Save-RecordedService -State $state",
        "$again = Get-RecordedService",
        "@{ count = $again.services.Count; path = $again.services['hvmcp_test'].image_path;"
        " start = $again.services['hvmcp_test'].start_type } | ConvertTo-Json -Compress",
    ])
    assert payload["count"] == 1
    assert payload["path"] == r"C:\DriverLab\Packages\abc\mydriver.sys"
    assert payload["start"] == "demand"


@WINDOWS_POWERSHELL_ONLY
def test_the_guest_names_the_states_the_same_way_the_host_does(tmp_path: Path) -> None:
    payload = run_guest_probe(tmp_path, [
        "$names = @{};",
        "foreach ($code in @(1, 2, 3, 4, 5, 6, 7, 99)) { $names[[string]$code] = ConvertTo-StateName -Code $code };",
        "$names | ConvertTo-Json -Compress",
    ])
    for code, name in guest_driver_service.STATES.items():
        assert payload[str(code)] == name
    assert payload["99"] == "unknown_99"


@WINDOWS_POWERSHELL_ONLY
def test_the_live_state_comes_from_the_service_manager_not_from_text(tmp_path: Path) -> None:
    """A missing service answers with its Win32 code; an existing one with its state.

    ``EventLog`` exists on every Windows install, and a status query on it needs
    no elevation, so this runs as an ordinary user.
    """
    payload = run_guest_probe(tmp_path, [
        "$scm = Open-Manager -Access 0x0001;",
        "$missing = Read-ServiceFact -Scm $scm -Name 'hvmcp_no_such_service_here';",
        "$present = Read-ServiceFact -Scm $scm -Name 'EventLog';",
        "[void][HvmcpScm.Native]::CloseServiceHandle($scm);",
        "@{ missing_exists = $missing.exists; missing_error = $missing.win32_error;"
        " present_exists = $present.exists; present_state = [string]$present.state }"
        " | ConvertTo-Json -Compress",
    ])
    assert payload["missing_exists"] is False
    assert payload["missing_error"] == 1060
    assert payload["present_exists"] is True
    assert payload["present_state"]
