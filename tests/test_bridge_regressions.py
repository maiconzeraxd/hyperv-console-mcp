from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from hyperv_console_mcp.backend import PowerShellBackend
from hyperv_console_mcp.errors import BackendError

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell.exe") is None,
    reason="Windows PowerShell is required.",
)

BRIDGE = Path(__file__).parents[1] / "src" / "hyperv_console_mcp" / "bridge" / "HyperVBridge.ps1"
BASE_MOCKS = """
Add-Type -TypeDefinition 'namespace Microsoft.HyperV.PowerShell { public enum VMState { Off = 0, Running = 1 } }'
function Import-Module { param($Name, $ErrorAction) }
function Get-VM {
    param($Name)
    [pscustomobject]@{ Name = 'NORTH-Test'; State = 'Running'; Generation = 2 }
}
"""
LOCAL_REMOTING = """
function Invoke-Command {
    param($VMName, $Credential, $ArgumentList, $ScriptBlock)
    & $ScriptBlock @ArgumentList
}
"""
GUEST_PAYLOAD = {
    "vm": "NORTH-Test", "username": "test-user", "password": "test-password",
    "max_output_chars": 65536,
}


def invoke_with_mocks(tmp_path: Path, mocks: str, operation: str, payload: dict) -> dict:
    escaped = str(BRIDGE).replace("'", "''")
    wrapper = tmp_path / "MockBridge.ps1"
    wrapper.write_text(
        f"param([switch]$Server)\n{BASE_MOCKS}\n{mocks}\n& '{escaped}' -Server\n",
        encoding="utf-8",
    )
    backend = PowerShellBackend(wrapper)
    try:
        return backend.invoke(operation, payload)
    finally:
        backend.close()


def test_checkpoint_create_preserves_the_passthru_snapshot(tmp_path: Path) -> None:
    result = invoke_with_mocks(
        tmp_path,
        """
function Checkpoint-VM {
    param($VM, $SnapshotName, [switch]$Passthru, [switch]$Confirm)
    if (-not $Passthru) { throw 'Passthru was not supplied.' }
    Write-Warning 'Synthetic checkpoint warning'
    [pscustomobject]@{
        Id = [guid]'6f4b3f13-2a9f-4eb0-a3f4-bd52e890c001'
        Name = $SnapshotName; CreationTime = [datetime]'2026-10-28T00:00:00Z'
        ParentSnapshotId = $null; Notes = 'mock'
    }
}
""",
        "checkpoint_create", {"vm": "NORTH-Test", "name": "before-lab"},
    )
    assert result["checkpoint"]["id"] == "6f4b3f13-2a9f-4eb0-a3f4-bd52e890c001"
    assert result["checkpoint"]["name"] == "before-lab"


def test_guest_exec_captures_native_stderr_and_strips_remoting_properties(tmp_path: Path) -> None:
    mocks = LOCAL_REMOTING.replace(
        "& $ScriptBlock @ArgumentList",
        """$value = & $ScriptBlock @ArgumentList
    $value | Select-Object *, @{ Name = 'PSComputerName'; Expression = { 'guest' } },
        @{ Name = 'RunspaceId'; Expression = { [guid]::Empty } },
        @{ Name = 'PSShowComputerName'; Expression = { $true } }""",
    )
    result = invoke_with_mocks(
        tmp_path, mocks, "guest_exec",
        {**GUEST_PAYLOAD, "command": "& cmd.exe /c \"echo native failure 1>&2 & exit /b 7\""},
    )
    execution = result["execution"]
    assert execution["success"] is False
    assert execution["exit_code"] == 7
    assert "native failure" in execution["output"]
    assert not {"PSComputerName", "RunspaceId", "PSShowComputerName"} & set(execution)


def test_guest_events_returns_an_empty_list_when_no_events_match(tmp_path: Path) -> None:
    result = invoke_with_mocks(
        tmp_path,
        LOCAL_REMOTING + """
function Get-WinEvent {
    param($FilterHashtable, $MaxEvents, $ErrorAction)
    Write-Error -Message 'No matching events' -ErrorId 'NoMatchingEventsFound' -ErrorAction Stop
}
""",
        "guest_events",
        {**GUEST_PAYLOAD, "log_name": "System", "max_events": 10, "hours": 24},
    )
    assert result["events"] == []


def test_secure_boot_preflight_prevents_any_bcdedit_mutation(tmp_path: Path) -> None:
    mocks = LOCAL_REMOTING + """
function Confirm-SecureBootUEFI { param($ErrorAction) return $true }
function bcdedit.exe { throw 'bcdedit must not be called by this test' }
if ((Get-Command bcdedit.exe).CommandType -ne 'Function') { throw 'Unsafe bcdedit mock.' }
"""
    with pytest.raises(BackendError, match="Secure Boot is enabled") as error:
        invoke_with_mocks(
            tmp_path, mocks, "debug_guest_prepare",
            {"vm": "NORTH-Test", "username": "test-user", "password": "test-password"},
        )
    assert error.value.code != "bridge_transport"


def test_secure_boot_preflight_runs_before_dbgsettings(tmp_path: Path) -> None:
    mocks = LOCAL_REMOTING + """
function Confirm-SecureBootUEFI { param($ErrorAction) return $false }
function bcdedit.exe { param($Arguments) throw 'bcdedit stopped safely by the mock' }
if ((Get-Command bcdedit.exe).CommandType -ne 'Function') { throw 'Unsafe bcdedit mock.' }
"""
    with pytest.raises(BackendError, match="stopped safely") as error:
        invoke_with_mocks(
            tmp_path, mocks, "debug_guest_prepare",
            {"vm": "NORTH-Test", "username": "test-user", "password": "test-password"},
        )
    assert error.value.code != "bridge_transport"


def test_unknown_operation_is_an_explicit_invalid_argument(tmp_path: Path) -> None:
    with pytest.raises(BackendError, match="Unsupported bridge operation") as error:
        invoke_with_mocks(tmp_path, "", "not_a_real_operation", {})
    assert error.value.code == "invalid_argument"


def test_wildcards_in_vm_names_are_escaped_before_lookup(tmp_path: Path) -> None:
    mocks = """
function Get-VM {
    param($Name)
    if ($Name -ne [WildcardPattern]::Escape('NORTH[Lab]')) { throw 'VM name was not escaped.' }
    [pscustomobject]@{ Name = 'NORTH[Lab]'; State = 'Running'; Generation = 2 }
}
function Get-VMComPort { param($Vm) }
"""
    result = invoke_with_mocks(
        tmp_path, mocks, "debug_serial_status", {"vm": "NORTH[Lab]"}
    )
    assert result["vm"] == "NORTH[Lab]"


def test_access_denied_uses_the_structured_hresult(tmp_path: Path) -> None:
    mocks = """
function Get-VM { param($Name) throw [System.UnauthorizedAccessException]::new('Mock denial') }
"""
    with pytest.raises(BackendError) as error:
        invoke_with_mocks(tmp_path, mocks, "debug_serial_status", {"vm": "NORTH-Test"})
    assert error.value.code == "access_denied"
