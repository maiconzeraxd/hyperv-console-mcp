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


# The guest query helper declares -Arguments. Four operations used to pass -ArgumentList,
# which failed parameter binding before the guest scriptblock ever ran.
BINDING_PROBE = """
function Invoke-Command {
    param($VMName, $Credential, $ArgumentList, $ScriptBlock)
    $names = @($ScriptBlock.Ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })
    [pscustomobject]@{
        bound = $true
        parameter_names = ($names -join ',')
        argument_count = @($ArgumentList).Count
    }
}
"""


@pytest.mark.parametrize(
    ("operation", "payload", "result_key", "parameter_names", "argument_count"),
    [
        (
            "guest_read_file",
            {"path": "C:\\DriverLab\\sample.bin", "max_bytes": 4096},
            "file", "EncodedPath,MaximumBytes", 2,
        ),
        (
            "guest_write_file",
            {
                "path": "C:\\DriverLab\\sample.bin", "content_base64": "AAECAw==",
                "overwrite": False, "create_parent": False,
            },
            "file", "EncodedPath,Content,Overwrite,CreateParent", 4,
        ),
        (
            "guest_read_chunk",
            {"path": "C:\\DriverLab\\sample.bin", "offset": 0, "count": 4},
            "chunk", "EncodedPath,Offset,Count", 3,
        ),
        (
            "guest_assemble_file",
            {
                "path": "C:\\DriverLab\\Packages\\pkg\\driver.sys",
                "parts_directory": "C:\\DriverLab\\Transfers\\t-1",
                "part_count": 2, "expected_size": 8,
                "expected_sha256": "0" * 64,
            },
            "file", "EncodedPath,EncodedParts,PartCount,ExpectedSize,ExpectedHash", 5,
        ),
        (
            "guest_publish_package",
            {
                "package_id": "pkg.1", "transfer_id": "0" * 32,
                "manifest_sha256": "0" * 64,
            },
            "package", "PackageId,TransferId,ExpectedHash", 3,
        ),
    ],
)
def test_guest_file_operations_bind_every_argument(
    tmp_path: Path,
    operation: str,
    payload: dict,
    result_key: str,
    parameter_names: str,
    argument_count: int,
) -> None:
    result = invoke_with_mocks(
        tmp_path, BINDING_PROBE, operation, {**GUEST_PAYLOAD, **payload}
    )
    bound = result[result_key]
    assert bound["bound"] is True
    assert bound["parameter_names"] == parameter_names
    assert bound["argument_count"] == argument_count


def test_guest_exec_reports_a_non_terminating_cmdlet_error_as_failure(tmp_path: Path) -> None:
    result = invoke_with_mocks(
        tmp_path, LOCAL_REMOTING, "guest_exec",
        {**GUEST_PAYLOAD, "command": "Get-Item C:\\hvmcp-does-not-exist-8f2c"},
    )
    execution = result["execution"]
    assert execution["success"] is False
    assert execution["had_errors"] is True
    assert execution["exit_code"] == 0
    assert execution["error_records"]
    assert any("hvmcp-does-not-exist-8f2c" in record for record in execution["error_records"])


def test_guest_exec_keeps_native_stderr_from_failing_a_successful_command(
    tmp_path: Path,
) -> None:
    result = invoke_with_mocks(
        tmp_path, LOCAL_REMOTING, "guest_exec",
        {**GUEST_PAYLOAD, "command": "& cmd.exe /c \"echo progress 1>&2 & exit /b 0\""},
    )
    execution = result["execution"]
    assert execution["success"] is True
    assert execution["had_errors"] is False
    assert execution["error_records"] == []
    assert "progress" in execution["output"]


# Remoting rethrows a guest error as a generic RuntimeException that carries the
# original exception, so the host has to unwrap it to keep not_found distinct.
REMOTED_FAILURE = """
function Invoke-Command {
    param($VMName, $Credential, $ArgumentList, $ScriptBlock)
    try { & $ScriptBlock @ArgumentList }
    catch { throw [System.Management.Automation.RuntimeException]::new($_.Exception.Message, $_.Exception) }
}
"""


def test_a_missing_guest_file_is_reported_as_not_found(tmp_path: Path) -> None:
    with pytest.raises(BackendError, match="Guest file not found") as error:
        invoke_with_mocks(
            tmp_path, REMOTED_FAILURE, "guest_read_file",
            {**GUEST_PAYLOAD, "path": "C:\\DriverLab\\hvmcp-missing-4d1a.bin", "max_bytes": 4096},
        )
    assert error.value.code == "not_found"


def test_a_generic_guest_failure_is_not_mistaken_for_a_missing_file(tmp_path: Path) -> None:
    mocks = """
function Invoke-Command {
    param($VMName, $Credential, $ArgumentList, $ScriptBlock)
    throw 'The guest is unreachable.'
}
"""
    with pytest.raises(BackendError, match="unreachable") as error:
        invoke_with_mocks(
            tmp_path, mocks, "guest_read_file",
            {**GUEST_PAYLOAD, "path": "C:\\DriverLab\\hvmcp-missing-4d1a.bin", "max_bytes": 4096},
        )
    assert error.value.code == "bridge_error"


FULL_VM_MOCK = """
function Get-VM {
    param($Name)
    [pscustomobject]@{
        Name = 'NORTH-Test'; Id = [guid]'11111111-2222-3333-4444-555555555555'
        State = [Microsoft.HyperV.PowerShell.VMState]::Running
        Status = 'Operating normally'; Generation = 2; Version = [version]'10.0'
        ProcessorCount = 2; MemoryStartup = [Int64]2147483648
        MemoryAssigned = [Int64]2147483648; Uptime = [timespan]::FromMinutes(5)
        CheckpointType = 'Production'; AutomaticCheckpointsEnabled = $true
        AutomaticStartAction = 'StartIfRunning'; AutomaticStopAction = 'Save'
    }
}
function Get-VMHardDiskDrive { param($VM) }
function Get-VMDvdDrive { param($VM) }
function Get-VMNetworkAdapter { param($VM) }
function Get-VMSnapshot { param($VM) }
function Stop-VM {
    param($VM, [switch]$Save, [switch]$TurnOff, [switch]$Force, [switch]$Confirm, [switch]$AsJob)
    # Forced power may stay synchronous; graceful stop/save must not block the worker.
    if (-not $AsJob -and -not $TurnOff) { throw 'Stop-VM blocked the bridge; it must run as a background job.' }
    [pscustomobject]@{
        State = 'Running'
        JobStateInfo = [pscustomobject]@{ Reason = $null }
        ChildJobs = @()
    }
}
"""


def test_graceful_shutdown_is_accepted_without_blocking_the_worker(tmp_path: Path) -> None:
    result = invoke_with_mocks(
        tmp_path, FULL_VM_MOCK, "vm_power", {"vm": "NORTH-Test", "action": "shutdown"}
    )
    assert result["accepted"] is True
    assert result["action"] == "shutdown"
    assert result["state"] == "running"
    assert len(result["operation_id"]) == 32
    assert result["vm"]["name"] == "NORTH-Test"


def test_operation_status_tracks_a_background_power_operation(tmp_path: Path) -> None:
    escaped = str(BRIDGE).replace("'", "''")
    wrapper = tmp_path / "MockBridge.ps1"
    wrapper.write_text(
        f"param([switch]$Server)\n{BASE_MOCKS}\n{FULL_VM_MOCK}\n& '{escaped}' -Server\n",
        encoding="utf-8",
    )
    backend = PowerShellBackend(wrapper)
    try:
        accepted = backend.invoke(
            "vm_power", {"vm": "NORTH-Test", "action": "save"}
        )
        status = backend.invoke(
            "operation_status", {"operation_id": accepted["operation_id"]}
        )
        unknown = backend.invoke(
            "operation_status", {"operation_id": "0" * 32}
        )
    finally:
        backend.close()
    assert accepted["accepted"] is True
    assert status["operation_id"] == accepted["operation_id"]
    assert status["state"] == "running"
    assert status["succeeded"] is None
    assert unknown["state"] == "unknown"


def test_forced_power_stays_synchronous(tmp_path: Path) -> None:
    result = invoke_with_mocks(
        tmp_path, FULL_VM_MOCK, "vm_power", {"vm": "NORTH-Test", "action": "turn_off"}
    )
    assert result["accepted"] is False
    assert result["operation_id"] is None
    assert result["state"] == "completed"
    assert result["vm"]["name"] == "NORTH-Test"
