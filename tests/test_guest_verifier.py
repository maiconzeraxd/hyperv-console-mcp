"""Driver Verifier: the arguments, the guest program, and what the numbers mean.

The guest is a fake transport here; the PowerShell tests dot-source the guest
script against a temporary state file and touch no VM.
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
from hyperv_console_mcp.errors import BackendError, ValidationError
from hyperv_console_mcp.transport import guest_verifier
from hyperv_console_mcp.transport.ssh import ERROR_MARKER, JSON_MARKER

VM = "LAB-Test"


def marker(payload: dict[str, Any]) -> str:
    body = json.dumps(payload)
    return JSON_MARKER + base64.b64encode(body.encode("utf-8")).decode("ascii")


class FakeTransport:
    name = "psdirect"

    def __init__(self, output: str | None = None) -> None:
        self.commands: list[str] = []
        self.uploads: list[str] = []
        self.output = output if output is not None else marker({"mode": "Enable", "live": {}})

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


def test_the_script_is_shipped_next_to_this_module() -> None:
    assert guest_verifier.GUEST_SCRIPT.is_file()
    assert guest_verifier.GUEST_SCRIPT.name == "guest_verifier.ps1"


def test_status_asks_for_the_state_file_the_revert_reads() -> None:
    transport = FakeTransport()
    guest_verifier.status(service_with(transport), VM)
    command = transport.commands[-1]
    assert "-Mode Status" in command
    assert guest_verifier.GUEST_STATE_PATH in command
    assert transport.uploads[-1].endswith("guest_verifier.ps1")


def test_enable_names_the_flags_and_the_drivers() -> None:
    transport = FakeTransport()
    guest_verifier.apply(
        service_with(transport), VM, "enable", drivers=["a.sys", "b.sys"], flags="standard"
    )
    command = transport.commands[-1]
    assert "-Mode Enable" in command
    assert "-Flags 'standard'" in command
    # One quoted token: `-File` hands a script its arguments as text, and a
    # comma-separated list is the only form that survives that.
    assert "-Drivers 'a.sys,b.sys'" in command


def test_enable_without_drivers_asks_for_every_driver() -> None:
    transport = FakeTransport()
    guest_verifier.apply(service_with(transport), VM, "enable", drivers=None, flags="all")
    command = transport.commands[-1]
    assert "-Drivers ''" in command
    assert "-Flags 'all'" in command


def test_a_mask_is_normalised_before_it_reaches_the_guest() -> None:
    assert guest_verifier.normalise_flags("0x9") == "0x00000009"
    assert guest_verifier.normalise_flags("9") == "0x00000009"
    assert guest_verifier.normalise_flags("STANDARD") == "standard"
    with pytest.raises(ValidationError):
        guest_verifier.normalise_flags("special-pool")
    with pytest.raises(ValidationError):
        guest_verifier.normalise_flags("0x1FFFFFFFF")


def test_a_driver_is_a_name_and_never_a_path() -> None:
    """A path in VerifyDrivers matches no loaded module, so it verifies nothing."""
    assert guest_verifier.normalise_drivers("a.sys, b.sys") == ["a.sys", "b.sys"]
    assert guest_verifier.normalise_drivers(["a.sys", "a.sys"]) == ["a.sys"]
    for bad in [r"C:\DriverLab\a.sys", r"..\a.sys", "a b.sys", "..", ""]:
        with pytest.raises(ValidationError):
            guest_verifier.normalise_drivers([bad])


def test_disable_and_revert_take_no_drivers() -> None:
    transport = FakeTransport()
    with pytest.raises(ValidationError):
        guest_verifier.apply(service_with(transport), VM, "disable", drivers=["a.sys"])
    assert transport.commands == []


def test_an_error_marker_becomes_a_backend_error() -> None:
    transport = FakeTransport(output=ERROR_MARKER + "verifier is not available")
    with pytest.raises(BackendError) as caught:
        guest_verifier.apply(service_with(transport), VM, "revert")
    assert caught.value.code == "guest_verifier_failed"


def test_the_mask_decides_whether_the_verifier_is_on() -> None:
    result = guest_verifier.derive({"live": {"flags_mask": 0x1BF}})
    assert result["enabled"] is True
    assert "special_pool" in result["live"]["flags_named"]
    assert result["live"]["unnamed_bits"] == []


def test_a_bit_without_a_documented_name_is_reported_by_number() -> None:
    """Naming a bit wrongly is worse than not naming it at all."""
    result = guest_verifier.derive({"live": {"flags_mask": 0x2001}})
    assert result["live"]["flags_named"] == ["special_pool"]
    assert result["live"]["unnamed_bits"] == ["0x00002000"]


def test_an_absent_setting_is_not_off() -> None:
    """A guest that never ran verifier has no value; that is not a zero mask."""
    result = guest_verifier.derive({"live": {"flags_mask": None}})
    assert result["enabled"] is False
    assert result["live"]["flags_hex"] is None


WINDOWS_POWERSHELL_ONLY = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell") is None,
    reason="needs Windows PowerShell 5.1 to run the guest script",
)


def run_guest_probe(tmp_path: Path, lines: list[str]) -> Any:
    """Dot-source the guest script in a fresh powershell.exe and run ``lines``."""
    script = guest_verifier.GUEST_SCRIPT
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
def test_a_recorded_state_is_read_back_and_can_be_extended(tmp_path: Path) -> None:
    """A second Enable reads the state file the first one wrote.

    The value comes back from ``ConvertFrom-Json`` as a PSCustomObject, which
    cannot be extended the way the record is used, so it is converted on read.
    """
    state_file = tmp_path / "verifier.json"
    payload = run_guest_probe(tmp_path, [
        f"$script:StateFile = '{state_file}'",
        "$state = @{ version = 1; entry = @{ flags_mask = 9; drivers = @('a.sys','b.sys');"
        " recorded_utc = '2026-10-29T00:00:00Z' } }",
        "Save-VerifierState -State $state",
        "$again = Get-VerifierState",
        "$again.entry.applied_boot_utc = '2026-10-29T01:00:00Z'",
        "Save-VerifierState -State $again",
        "$final = Get-VerifierState",
        "@{ mask = $final.entry.flags_mask; drivers = @($final.entry.drivers).Count;"
        " boot = $final.entry.applied_boot_utc } | ConvertTo-Json -Compress",
    ])
    assert payload["mask"] == 9
    assert payload["drivers"] == 2
    assert payload["boot"] == "2026-10-29T01:00:00Z"


@WINDOWS_POWERSHELL_ONLY
def test_the_guest_parses_a_mask_the_same_way_the_host_does(tmp_path: Path) -> None:
    payload = run_guest_probe(tmp_path, [
        "$values = @{};",
        "foreach ($text in @('0x9', '9', '0x00000009')) {",
        "  $values[$text] = ConvertTo-FlagsMask -Value $text };",
        "$rejected = 0;",
        "foreach ($text in @('special-pool', '0x1FFFFFFFF', '-1', '')) {",
        "  try { $null = ConvertTo-FlagsMask -Value $text } catch { $rejected++ } };",
        "@{ values = $values; rejected = $rejected } | ConvertTo-Json -Compress",
    ])
    assert payload["values"] == {"0x9": 9, "9": 9, "0x00000009": 9}
    assert payload["rejected"] == 4


@WINDOWS_POWERSHELL_ONLY
def test_the_guest_refuses_a_driver_path_too(tmp_path: Path) -> None:
    payload = run_guest_probe(tmp_path, [
        "$accepted = 0;",
        "$rejected = 0;",
        "foreach ($text in @('a.sys,b.sys', 'a.sys, b.sys')) {",
        "  if (@(Get-DriverName -Drivers $text).Count -eq 2) { $accepted++ } };",
        "foreach ($text in @('..\\a.sys', 'a b.sys', '..', 'a/b.sys')) {",
        "  try { $null = Get-DriverName -Drivers $text } catch { $rejected++ } };",
        "@{ accepted = $accepted; rejected = $rejected } | ConvertTo-Json -Compress",
    ])
    assert payload["accepted"] == 2
    assert payload["rejected"] == 4


@WINDOWS_POWERSHELL_ONLY
def test_the_live_facts_are_read_without_parsing_verifier_output(tmp_path: Path) -> None:
    """Read-VerifierFact reads two registry values and the boot time.

    It must answer on a machine where verifier has never run, which is the
    normal case: no VerifyDriverLevel, no VerifyDrivers, and no error.
    """
    payload = run_guest_probe(tmp_path, [
        "$facts = Read-VerifierFact",
        "@{ keys = @($facts.Keys | Sort-Object);"
        " drivers = @($facts.drivers).Count; boot = [string]$facts.boot_utc }"
        " | ConvertTo-Json -Compress",
    ])
    assert payload["keys"] == ["boot_utc", "drivers", "drivers_raw", "flags_mask"]
    assert payload["boot"]
