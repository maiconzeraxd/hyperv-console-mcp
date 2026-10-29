from __future__ import annotations

import base64
import hashlib
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
# Guest access reuses one PSSession per VM, so the mocks model that session.
function New-PSSession { param($VMName, $Credential) [pscustomobject]@{ State = 'Opened'; Id = 1 } }
function Remove-PSSession { param($Session, $ErrorAction) }
"""
LOCAL_REMOTING = """
function Invoke-Command {
    param($Session, $ArgumentList, $ScriptBlock)
    if ([string]$Session.State -ne 'Opened') { throw 'The remote session is not open.' }
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
function Get-VMSnapshot {
    param($VM)
    [pscustomobject]@{
        Id = [guid]'6f4b3f13-2a9f-4eb0-a3f4-bd52e890c001'
        Name = 'before-lab'; CreationTime = [datetime]'2026-10-28T00:00:00Z'
        ParentSnapshotId = $null; Notes = 'mock'
    }
}
""",
        "checkpoint_create", {"vm": "NORTH-Test", "name": "before-lab"},
    )
    assert result["checkpoint"]["id"] == "6f4b3f13-2a9f-4eb0-a3f4-bd52e890c001"
    assert result["checkpoint"]["name"] == "before-lab"


def test_checkpoint_create_waits_until_the_new_snapshot_is_listed(tmp_path: Path) -> None:
    """Hyper-V can list a checkpoint only after Checkpoint-VM has returned.

    Regression: on a live VM the checkpoint was created and the revert that
    followed found "0" checkpoints with that id, so the revert did nothing.
    """
    log = tmp_path / "snapshots.log"
    mocks = """
$global:SnapshotLog = '{log}'
$global:SnapshotCalls = 0
function Checkpoint-VM {
    param($VM, $SnapshotName, [switch]$Passthru, [switch]$Confirm)
    [pscustomobject]@{
        Id = [guid]'6f4b3f13-2a9f-4eb0-a3f4-bd52e890c001'
        Name = $SnapshotName; CreationTime = [datetime]'2026-10-28T00:00:00Z'
        ParentSnapshotId = $null; Notes = 'mock'
    }
}
function Get-VMSnapshot {
    param($VM)
    $global:SnapshotCalls++
    Add-Content -LiteralPath $global:SnapshotLog -Value "list $($global:SnapshotCalls)"
    if ($global:SnapshotCalls -le 1) { return }
    [pscustomobject]@{
        Id = [guid]'6f4b3f13-2a9f-4eb0-a3f4-bd52e890c001'
        Name = 'before-lab'; CreationTime = [datetime]'2026-10-28T00:00:00Z'
        ParentSnapshotId = $null; Notes = 'mock'
    }
}
""".replace("{log}", str(log).replace("'", "''"))
    result = invoke_with_mocks(
        tmp_path, mocks, "checkpoint_create", {"vm": "NORTH-Test", "name": "before-lab"},
    )
    assert result["checkpoint"]["id"] == "6f4b3f13-2a9f-4eb0-a3f4-bd52e890c001"
    assert len(log.read_text(encoding="utf-8").splitlines()) >= 2


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
    param($Session, $ArgumentList, $ScriptBlock)
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
    param($Session, $ArgumentList, $ScriptBlock)
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


def test_guest_file_info_reports_the_size_of_an_existing_file(tmp_path: Path) -> None:
    payload = tmp_path / "probe.bin"
    payload.write_bytes(b"0123456789")
    result = invoke_with_mocks(
        tmp_path, LOCAL_REMOTING, "guest_file_info",
        {**GUEST_PAYLOAD, "path": str(payload)},
    )
    assert result["file"]["status"] == "ok"
    assert result["file"]["size"] == 10
    assert result["file"]["path"] == str(payload)


def test_guest_file_info_reports_absence_as_not_found(tmp_path: Path) -> None:
    missing = tmp_path / "hvmcp-missing-4d1a.bin"
    with pytest.raises(BackendError, match="Guest file not found") as error:
        invoke_with_mocks(
            tmp_path, LOCAL_REMOTING, "guest_file_info",
            {**GUEST_PAYLOAD, "path": str(missing)},
        )
    assert error.value.code == "not_found"


def test_guest_write_file_replaces_an_existing_file(tmp_path: Path) -> None:
    """`File.Replace` needs a real null for its backup path.

    Regression: `[IO.File]::Replace($temp, $path, $null)` coerced `$null` to `''`
    for the [string] parameter and threw "The path has an invalid format", so
    overwriting any guest file failed — including reinstalling a lab tool.
    """
    target = tmp_path / "tool.ps1"
    target.write_bytes(b"old")
    content = b"new content"
    result = invoke_with_mocks(
        tmp_path, LOCAL_REMOTING, "guest_write_file",
        {
            **GUEST_PAYLOAD, "path": str(target),
            "content_base64": base64.b64encode(content).decode("ascii"),
            "overwrite": True, "create_parent": False,
        },
    )
    assert target.read_bytes() == content
    assert result["file"]["bytes_written"] == len(content)
    assert result["file"]["sha256"] == hashlib.sha256(content).hexdigest()


# Copy-Item has no session to talk to in these tests, so it degrades to a local copy.
SESSION_COPY = LOCAL_REMOTING + """
function Copy-Item {
    param($LiteralPath, $Destination, $ToSession, $FromSession, [switch]$Force)
    Microsoft.PowerShell.Management\\Copy-Item -LiteralPath $LiteralPath -Destination $Destination -Force
}
"""


def test_guest_put_file_copies_a_host_path_and_verifies_the_guest_digest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "driver.sys"
    source.write_bytes(b"MZ" + bytes(range(64)))
    target = tmp_path / "guest" / "driver.sys"
    result = invoke_with_mocks(
        tmp_path, SESSION_COPY, "guest_put_file",
        {
            **GUEST_PAYLOAD, "local_path": str(source),
            "guest_path": str(target), "overwrite": False,
        },
    )
    assert target.read_bytes() == source.read_bytes()
    assert result["file"]["size"] == 66
    assert result["file"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_guest_put_file_refuses_to_replace_an_existing_guest_file(tmp_path: Path) -> None:
    source = tmp_path / "host" / "driver.sys"
    source.parent.mkdir()
    source.write_bytes(b"new")
    target = tmp_path / "guest" / "driver.sys"
    target.parent.mkdir()
    target.write_bytes(b"previous")
    with pytest.raises(BackendError, match="already exists"):
        invoke_with_mocks(
            tmp_path, SESSION_COPY, "guest_put_file",
            {
                **GUEST_PAYLOAD, "local_path": str(source),
                "guest_path": str(target), "overwrite": False,
            },
        )
    assert target.read_bytes() == b"previous"


def test_guest_put_file_rejects_a_guest_that_received_different_bytes(tmp_path: Path) -> None:
    source = tmp_path / "host" / "driver.sys"
    source.parent.mkdir()
    source.write_bytes(b"MZ")
    target = tmp_path / "guest" / "driver.sys"
    tampered = SESSION_COPY + """
function Copy-Item {
    param($LiteralPath, $Destination, $ToSession, $FromSession, [switch]$Force)
    [IO.File]::WriteAllText($Destination, 'tampered')
}
"""
    with pytest.raises(BackendError, match="different SHA-256"):
        invoke_with_mocks(
            tmp_path, tampered, "guest_put_file",
            {
                **GUEST_PAYLOAD, "local_path": str(source),
                "guest_path": str(target), "overwrite": True,
            },
        )


def test_guest_get_file_leaves_no_partial_download_behind(tmp_path: Path) -> None:
    guest_file = tmp_path / "guest" / "dump.bin"
    guest_file.parent.mkdir()
    guest_file.write_bytes(b"A" * 32)
    target = tmp_path / "host" / "dump.bin"
    failing = SESSION_COPY + """
function Copy-Item {
    param($LiteralPath, $Destination, $ToSession, $FromSession, [switch]$Force)
    [IO.File]::WriteAllText($Destination, 'short')
    throw 'The transfer was interrupted.'
}
"""
    with pytest.raises(BackendError, match="interrupted"):
        invoke_with_mocks(
            tmp_path, failing, "guest_get_file",
            {**GUEST_PAYLOAD, "guest_path": str(guest_file), "local_path": str(target),
             "overwrite": True},
        )
    assert not target.exists()
    assert not target.with_name("dump.bin.part").exists()


def test_guest_get_file_downloads_and_verifies_the_copy(tmp_path: Path) -> None:
    guest_file = tmp_path / "guest" / "dump.bin"
    guest_file.parent.mkdir()
    guest_file.write_bytes(b"B" * 48)
    target = tmp_path / "host" / "dump.bin"
    result = invoke_with_mocks(
        tmp_path, SESSION_COPY, "guest_get_file",
        {**GUEST_PAYLOAD, "guest_path": str(guest_file), "local_path": str(target),
         "overwrite": True},
    )
    assert target.read_bytes() == b"B" * 48
    assert result["file"]["sha256"] == hashlib.sha256(b"B" * 48).hexdigest()


SESSION_REUSE = LOCAL_REMOTING + """
$global:SessionLog = '{log}'
$global:GetVmCalls = 0
function New-PSSession {
    param($VMName, $Credential)
    Add-Content -LiteralPath $global:SessionLog -Value 'new'
    [pscustomobject]@{ State = 'Opened'; Id = 1 }
}
function Get-VM {
    param($Name)
    $global:GetVmCalls++
    # The third request reports a lower uptime, which means the guest rebooted.
    $uptime = if ($global:GetVmCalls -le 2) { [TimeSpan]::FromMinutes(30) } else { [TimeSpan]::FromMinutes(2) }
    [pscustomobject]@{
        Name = 'NORTH-Test'; State = 'Running'; Generation = 2; Uptime = $uptime
    }
}
"""


def test_one_session_is_reused_until_the_guest_reboots(tmp_path: Path) -> None:
    log = tmp_path / "sessions.log"
    escaped = str(log).replace("'", "''")
    mocks = SESSION_REUSE.replace("{log}", escaped)
    escaped_bridge = str(BRIDGE).replace("'", "''")
    wrapper = tmp_path / "MockBridge.ps1"
    wrapper.write_text(
        f"param([switch]$Server)\n{BASE_MOCKS}\n{mocks}\n& '{escaped_bridge}' -Server\n",
        encoding="utf-8",
    )
    backend = PowerShellBackend(wrapper)
    try:
        for _ in range(3):
            backend.invoke("guest_exec", {**GUEST_PAYLOAD, "command": "hostname"})
    finally:
        backend.close()
    # Two requests share the first session; the rebooted third one starts over.
    assert log.read_text(encoding="utf-8").split() == ["new", "new"]



def test_a_generic_guest_failure_is_not_mistaken_for_a_missing_file(tmp_path: Path) -> None:
    mocks = """
function Invoke-Command {
    param($Session, $ArgumentList, $ScriptBlock)
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

SSH_NETWORK_MOCKS = """
$MockLogPath = '{log}'
function Add-MockLog { param([string]$Value) Add-Content -LiteralPath $MockLogPath -Value $Value }
function Get-VMSwitch { param($Name, $ErrorAction) }
function New-VMSwitch {
    param($Name, $SwitchType, $ErrorAction)
    Add-MockLog "New-VMSwitch:${Name}:${SwitchType}"
}
function Get-VMNetworkAdapter { param($VM) }
function Add-VMNetworkAdapter {
    param($VM, $Name, $SwitchName, $ErrorAction)
    Add-MockLog "Add-VMNetworkAdapter:${Name}:${SwitchName}"
    [pscustomobject]@{ Name = $Name; SwitchName = $SwitchName }
}
function Connect-VMNetworkAdapter {
    param($VMNetworkAdapter, $SwitchName, $ErrorAction)
    Add-MockLog "Connect-VMNetworkAdapter:$($VMNetworkAdapter.Name):${SwitchName}"
}
function Remove-VMNetworkAdapter {
    param($VMNetworkAdapter, [switch]$Confirm, $ErrorAction)
    Add-MockLog "Remove-VMNetworkAdapter:$($VMNetworkAdapter.Name)"
}
function Disconnect-VMNetworkAdapter {
    param($VMNetworkAdapter, $ErrorAction)
    Add-MockLog "Disconnect-VMNetworkAdapter:$($VMNetworkAdapter.Name)"
}
function Get-NetIPAddress {
    param($IPAddress, $InterfaceAlias, $AddressFamily, $ErrorAction)
}
function New-NetIPAddress {
    param($IPAddress, $PrefixLength, $InterfaceAlias, $ErrorAction)
    Add-MockLog "New-NetIPAddress:$IPAddress/$PrefixLength@$InterfaceAlias"
}
function Remove-NetIPAddress {
    param($IPAddress, [switch]$Confirm, $ErrorAction)
    Add-MockLog "Remove-NetIPAddress:$IPAddress"
}
"""

SSH_NETWORK_PAYLOAD = {
    "vm": "NORTH-Test", "switch_name": "HyperV-MCP-Lab",
    "host_ip": "10.77.0.1", "prefix_length": 24,
    "adapter_name": "HyperV-MCP-Lab",
}

def _ssh_network_mocks(tmp_path: Path, extra: str = "") -> str:
    log = tmp_path / "network.log"
    return SSH_NETWORK_MOCKS.replace("{log}", str(log).replace("'", "''")) + extra

def _read_log(tmp_path: Path) -> list[str]:
    path = tmp_path / "network.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

def test_ssh_network_prepare_creates_only_an_internal_switch(tmp_path: Path) -> None:
    result = invoke_with_mocks(
        tmp_path, _ssh_network_mocks(tmp_path), "ssh_network_prepare", SSH_NETWORK_PAYLOAD
    )
    assert result["switch_created"] is True
    assert result["adapter_created"] is True
    assert result["host_address_created"] is True
    assert result["adapter_name"] == "HyperV-MCP-Lab"
    assert _read_log(tmp_path) == [
        "New-VMSwitch:HyperV-MCP-Lab:Internal",
        "Add-VMNetworkAdapter:HyperV-MCP-Lab:HyperV-MCP-Lab",
        "New-NetIPAddress:10.77.0.1/24@vEthernet (HyperV-MCP-Lab)",
    ]

def test_ssh_network_prepare_refuses_an_external_switch(tmp_path: Path) -> None:
    extra = """
function Get-VMSwitch { param($Name, $ErrorAction) [pscustomobject]@{ Name = $Name; SwitchType = 'External' } }
"""
    with pytest.raises(BackendError, match="not Internal"):
        invoke_with_mocks(
            tmp_path, _ssh_network_mocks(tmp_path, extra), "ssh_network_prepare",
            SSH_NETWORK_PAYLOAD,
        )
    assert _read_log(tmp_path) == []

def test_ssh_network_prepare_refuses_to_hijack_an_existing_adapter(tmp_path: Path) -> None:
    extra = """
function Get-VMNetworkAdapter { param($VM) [pscustomobject]@{ Name = 'WAN'; SwitchName = 'Default Switch' } }
"""
    # The guard's own words: a looser match would also accept the StrictMode error
    # "the property 'use_existing_adapter' was not found", which is not the guard.
    with pytest.raises(BackendError, match="already has a network adapter"):
        invoke_with_mocks(
            tmp_path, _ssh_network_mocks(tmp_path, extra), "ssh_network_prepare",
            SSH_NETWORK_PAYLOAD,
        )
    assert _read_log(tmp_path) == []

def test_ssh_network_prepare_can_attach_an_existing_adapter(tmp_path: Path) -> None:
    extra = """
function Get-VMNetworkAdapter { param($VM) [pscustomobject]@{ Name = 'WAN'; SwitchName = 'Default Switch' } }
"""
    result = invoke_with_mocks(
        tmp_path, _ssh_network_mocks(tmp_path, extra), "ssh_network_prepare",
        {**SSH_NETWORK_PAYLOAD, "use_existing_adapter": True},
    )
    assert result["adapter_created"] is False
    assert result["adapter_name"] == "WAN"
    # The switch it left behind is recorded so deprovision can put it back.
    assert result["previous_switch"] == "Default Switch"
    assert "Connect-VMNetworkAdapter:WAN:HyperV-MCP-Lab" in _read_log(tmp_path)

def test_ssh_network_prepare_reuses_an_existing_host_address(tmp_path: Path) -> None:
    extra = """
function Get-NetIPAddress {
    param($IPAddress, $InterfaceAlias, $AddressFamily, $ErrorAction)
    [pscustomobject]@{ IPAddress = '10.77.0.1'; PrefixLength = 24 }
}
"""
    result = invoke_with_mocks(
        tmp_path, _ssh_network_mocks(tmp_path, extra), "ssh_network_prepare",
        SSH_NETWORK_PAYLOAD,
    )
    assert result["host_address_created"] is False
    assert not [line for line in _read_log(tmp_path) if line.startswith("New-NetIPAddress")]

def test_ssh_network_remove_undoes_only_what_was_added(tmp_path: Path) -> None:
    extra = """
function Get-VMNetworkAdapter {
    param($VM) [pscustomobject]@{ Name = 'HyperV-MCP-Lab'; SwitchName = 'HyperV-MCP-Lab' }
}
function Get-NetIPAddress {
    param($IPAddress, $InterfaceAlias, $AddressFamily, $ErrorAction)
    [pscustomobject]@{ IPAddress = '10.77.0.1'; PrefixLength = 24 }
}
"""
    result = invoke_with_mocks(
        tmp_path, _ssh_network_mocks(tmp_path, extra), "ssh_network_remove",
        {**SSH_NETWORK_PAYLOAD, "remove_adapter": True, "remove_host_address": True},
    )
    assert result["removed"] == ["adapter", "host_address"]
    assert _read_log(tmp_path) == [
        "Remove-VMNetworkAdapter:HyperV-MCP-Lab",
        "Remove-NetIPAddress:10.77.0.1",
    ]

def test_ssh_network_remove_keeps_a_foreign_switch_alone(tmp_path: Path) -> None:
    """With nothing recorded, the adapter is left where it is.

    Regression: it was disconnected, so a VM whose only adapter had been moved
    onto the lab switch by an earlier provisioning ended up with no network at
    all — and with no way to tell from the tool what had happened.
    """
    extra = """
function Get-VMNetworkAdapter {
    param($VM) [pscustomobject]@{ Name = 'HyperV-MCP-Lab'; SwitchName = 'HyperV-MCP-Lab' }
}
"""
    result = invoke_with_mocks(
        tmp_path, _ssh_network_mocks(tmp_path, extra), "ssh_network_remove",
        {**SSH_NETWORK_PAYLOAD, "remove_adapter": False, "remove_host_address": False},
    )
    assert result["removed"] == ["connection-kept"]
    assert _read_log(tmp_path) == []

def test_ssh_network_remove_puts_a_reused_adapter_back_on_its_switch(tmp_path: Path) -> None:
    """Deprovision restores the switch a reused adapter came from.

    Regression: it only disconnected the adapter, so a VM that arrived on another
    switch left the lab with no network at all.
    """
    extra = """
function Get-VMNetworkAdapter {
    param($VM) [pscustomobject]@{ Name = 'WAN'; SwitchName = 'HyperV-MCP-Lab' }
}
function Get-VMSwitch {
    param($Name, $ErrorAction)
    [pscustomobject]@{ Name = $Name; SwitchType = 'Internal' }
}
"""
    result = invoke_with_mocks(
        tmp_path, _ssh_network_mocks(tmp_path, extra), "ssh_network_remove",
        {**SSH_NETWORK_PAYLOAD, "adapter_name": "WAN", "remove_adapter": False,
         "remove_host_address": False, "previous_switch": "Default Switch"},
    )
    assert result["removed"] == ["connection:Default Switch"]
    assert _read_log(tmp_path) == ["Connect-VMNetworkAdapter:WAN:Default Switch"]

def test_ssh_network_remove_keeps_the_adapter_when_the_old_switch_is_gone(
    tmp_path: Path,
) -> None:
    """A recorded switch that no longer exists must not fail the deprovision.

    It also must not disconnect the adapter: the switch cannot be restored, and
    leaving the connection in place is the only recoverable outcome.
    """
    extra = """
function Get-VMNetworkAdapter {
    param($VM) [pscustomobject]@{ Name = 'WAN'; SwitchName = 'HyperV-MCP-Lab' }
}
"""
    result = invoke_with_mocks(
        tmp_path, _ssh_network_mocks(tmp_path, extra), "ssh_network_remove",
        {**SSH_NETWORK_PAYLOAD, "adapter_name": "WAN", "remove_adapter": False,
         "remove_host_address": False, "previous_switch": "Default Switch"},
    )
    assert result["removed"] == ["connection-kept"]
    assert _read_log(tmp_path) == []

PASSWORD_PAYLOAD = {
    "vm": "NORTH-Test", "username": "test-user", "password": "test-password",
    "new_password": "N3w-Passw0rd!Value",
}

def test_guest_set_password_never_puts_the_secret_in_a_command(tmp_path: Path) -> None:
    mocks = LOCAL_REMOTING + """
$global:HvmcpSeen = @{}
function Set-LocalUser {
    param($Name, $Password)
    $global:HvmcpSeen.Name = $Name
    $global:HvmcpSeen.PlainLength = $Password.Length
}
function Enable-LocalUser { param($Name) $global:HvmcpSeen.Enabled = $Name }
function Get-LocalUser {
    param($Name)
    [pscustomobject]@{ Name = $Name; Enabled = $true; PasswordLastSet = [datetime]'2026-10-29T00:00:00Z' }
}
"""
    result = invoke_with_mocks(tmp_path, mocks, "guest_set_password", PASSWORD_PAYLOAD)
    account = result["account"]
    assert account["account"] == "test-user"
    assert account["enabled"] is True
    # The secret reached the guest as a script-block argument, so nothing in the
    # result echoes it and no command string was built.
    assert "new_password" not in result
    assert "N3w-Passw0rd" not in str(result)

def test_guest_auth_check_reports_a_rejected_password(tmp_path: Path) -> None:
    mocks = LOCAL_REMOTING + """
function New-PSSession { param($VMName, $Credential) throw 'The user name or password is incorrect.' }
"""
    result = invoke_with_mocks(tmp_path, mocks, "guest_auth_check", PASSWORD_PAYLOAD)
    assert result["ok"] is False
    assert "incorrect" in result["message"]

def test_guest_auth_check_reports_a_working_password(tmp_path: Path) -> None:
    result = invoke_with_mocks(tmp_path, LOCAL_REMOTING, "guest_auth_check", PASSWORD_PAYLOAD)
    assert result["ok"] is True
    assert result["message"] == ""


def test_vm_firmware_survives_a_host_that_omits_the_preferred_boot_device(
    tmp_path: Path,
) -> None:
    """Hyper-V leaves PreferredBootDevice out entirely when nothing is preferred.

    Regression: the bridge read ``$firmware.PreferredBootDevice`` directly, and
    strict mode turned the missing property into a fatal error, so every
    lab_guest_configure call died before it touched the guest.
    """
    mocks = """
function Get-VMFirmware {
    param($VM)
    [pscustomobject]@{ SecureBoot = 'On' }
}
"""
    result = invoke_with_mocks(tmp_path, mocks, "vm_firmware", {"vm": "NORTH-Test"})
    assert result["generation"] == 2
    assert result["secure_boot"] == "On"
    assert result["preferred_boot_device"] is None


def test_vm_firmware_reports_the_preferred_boot_device_when_it_is_set(
    tmp_path: Path,
) -> None:
    mocks = """
function Get-VMFirmware {
    param($VM)
    [pscustomobject]@{ SecureBoot = 'Off'; PreferredBootDevice = 'Drive:0' }
}
"""
    result = invoke_with_mocks(tmp_path, mocks, "vm_firmware", {"vm": "NORTH-Test"})
    assert result["preferred_boot_device"] == "Drive:0"
