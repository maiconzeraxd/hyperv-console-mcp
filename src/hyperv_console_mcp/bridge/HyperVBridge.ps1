[CmdletBinding()]
param(
    [string]$Operation,
    [switch]$Server
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

# Background Hyper-V jobs (graceful shutdown / save) survive across requests in this worker.
$script:PowerOperations = @{}

function Write-Response {
    param([Parameter(Mandatory)][hashtable]$Response)
    $json = ConvertTo-Json -InputObject $Response -Depth 8 -Compress
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
    [Console]::Out.WriteLine("HVMCP1:$encoded")
}

function Get-ExactVm {
    param([Parameter(Mandatory)][string]$Name)
    $escapedName = [WildcardPattern]::Escape($Name)
    $resolved = @(Get-VM -Name $escapedName | Where-Object { $_.Name -eq $Name })
    if ($resolved.Count -ne 1) {
        throw "Expected exactly one VM named '$Name'; found $($resolved.Count)."
    }
    return $resolved[0]
}

function Convert-Date {
    param($Value)
    if ($null -eq $Value) { return $null }
    return ([DateTime]$Value).ToString('o')
}

function Convert-Vm {
    param([Parameter(Mandatory)]$Vm)
    $disks = @(
        Get-VMHardDiskDrive -VM $Vm | ForEach-Object {
            [ordered]@{
                controller_number = $_.ControllerNumber
                controller_location = $_.ControllerLocation
                path = $_.Path
            }
        }
    )
    $dvds = @(
        Get-VMDvdDrive -VM $Vm | ForEach-Object {
            [ordered]@{
                controller_number = $_.ControllerNumber
                controller_location = $_.ControllerLocation
                path = $_.Path
            }
        }
    )
    $adapters = @(
        Get-VMNetworkAdapter -VM $Vm | ForEach-Object {
            [ordered]@{
                name = $_.Name
                switch_name = $_.SwitchName
                status = $_.Status
                ip_addresses = @($_.IPAddresses)
            }
        }
    )
    return [ordered]@{
        id = $Vm.Id.ToString()
        name = $Vm.Name
        state = $Vm.State.ToString()
        state_code = [int]$Vm.State
        status = $Vm.Status
        generation = [int]$Vm.Generation
        version = $Vm.Version.ToString()
        processor_count = [int]$Vm.ProcessorCount
        memory_startup_bytes = [Int64]$Vm.MemoryStartup
        memory_assigned_bytes = [Int64]$Vm.MemoryAssigned
        uptime_seconds = [Math]::Round($Vm.Uptime.TotalSeconds, 3)
        checkpoint_type = $Vm.CheckpointType.ToString()
        automatic_checkpoints_enabled = [bool]$Vm.AutomaticCheckpointsEnabled
        automatic_start_action = $Vm.AutomaticStartAction.ToString()
        automatic_stop_action = $Vm.AutomaticStopAction.ToString()
        checkpoint_count = @(Get-VMSnapshot -VM $Vm).Count
        hard_disks = $disks
        dvd_drives = $dvds
        network_adapters = $adapters
    }
}

function Get-VmCim {
    param([Parameter(Mandatory)]$Vm)
    $id = $Vm.Id.ToString()
    $instances = @(
        Get-CimInstance -Namespace 'root/virtualization/v2' -ClassName 'Msvm_ComputerSystem' `
            -Filter "Name = '$id'"
    )
    if ($instances.Count -ne 1) {
        throw "Could not resolve the Hyper-V WMI computer system for '$($Vm.Name)'."
    }
    return $instances[0]
}

function Get-InputDevice {
    param(
        [Parameter(Mandatory)]$Vm,
        [Parameter(Mandatory)][ValidateSet('Msvm_Keyboard', 'Msvm_SyntheticMouse')][string]$ClassName
    )
    $vmCim = Get-VmCim -Vm $Vm
    $devices = @(
        Get-CimAssociatedInstance -InputObject $vmCim -Association 'Msvm_SystemDevice' `
            -ResultClassName $ClassName
    )
    if ($devices.Count -ne 1) {
        throw "Expected one $ClassName for '$($Vm.Name)'; found $($devices.Count)."
    }
    return $devices[0]
}

function Convert-KeyName {
    param([Parameter(Mandatory)][string]$Name)
    $codes = @{
        ctrl = 0x11; shift = 0x10; alt = 0x12; win = 0x5B; enter = 0x0D
        escape = 0x1B; tab = 0x09; space = 0x20; backspace = 0x08
        delete = 0x2E; insert = 0x2D; home = 0x24; end = 0x23
        pageup = 0x21; pagedown = 0x22; up = 0x26; down = 0x28
        left = 0x25; right = 0x27
    }
    1..12 | ForEach-Object { $codes["f$_"] = 0x70 + $_ - 1 }
    0..9 | ForEach-Object { $codes["$_"] = 0x30 + $_ }
    0..25 | ForEach-Object { $codes[[char](0x61 + $_)] = 0x41 + $_ }
    if (-not $codes.ContainsKey($Name)) { throw "Unsupported key name '$Name'." }
    return [uint32]$codes[$Name]
}

function Invoke-InputMethod {
    param(
        [Parameter(Mandatory)]$Device,
        [Parameter(Mandatory)][string]$Method,
        [hashtable]$Arguments = @{}
    )
    $response = Invoke-CimMethod -InputObject $Device -MethodName $Method -Arguments $Arguments
    $returnValue = [uint32]$response.ReturnValue
    if ($returnValue -ne 0) {
        throw "Hyper-V WMI method $Method returned $returnValue."
    }
}

function Convert-Checkpoint {
    param([Parameter(Mandatory)]$Snapshot)
    return [ordered]@{
        id = $Snapshot.Id.ToString()
        name = $Snapshot.Name
        creation_time = Convert-Date $Snapshot.CreationTime
        parent_snapshot_id = if ($null -eq $Snapshot.ParentSnapshotId) { $null } else { $Snapshot.ParentSnapshotId.ToString() }
        notes = $Snapshot.Notes
    }
}

function Get-ExactCheckpoint {
    param([Parameter(Mandatory)]$Vm, [Parameter(Mandatory)][string]$SnapshotId)
    $snapshots = @(Get-VMSnapshot -VM $Vm | Where-Object { $_.Id.ToString() -eq $SnapshotId })
    if ($snapshots.Count -ne 1) {
        throw "Expected checkpoint '$SnapshotId' on '$($Vm.Name)'; found $($snapshots.Count)."
    }
    return $snapshots[0]
}

function New-GuestCredential {
    param([Parameter(Mandatory)]$Request)
    $secure = ConvertTo-SecureString $Request.password -AsPlainText -Force
    return [PSCredential]::new([string]$Request.username, $secure)
}

function Convert-ToBase64Text {
    param([AllowEmptyString()][string]$Text)
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Text))
}

function Invoke-GuestQuery {
    param(
        [Parameter(Mandatory)]$Vm,
        [Parameter(Mandatory)]$Request,
        [Parameter(Mandatory)][scriptblock]$Query,
        [Alias('ArgumentList')][object[]]$Arguments = @()
    )
    $credential = New-GuestCredential -Request $Request
    return Invoke-Command -VMName $Vm.Name -Credential $credential -ArgumentList $Arguments -ScriptBlock $Query |
        Select-Object -Property * -ExcludeProperty PSComputerName, RunspaceId, PSShowComputerName
}

function Start-BackgroundVmPower {
    param([Parameter(Mandatory)]$Vm, [Parameter(Mandatory)][string]$Action)
    $job = switch ($Action) {
        'shutdown' { Stop-VM -VM $Vm -Confirm:$false -AsJob }
        'save' { Stop-VM -VM $Vm -Save -Confirm:$false -AsJob }
        default { throw "Unsupported background power action '$Action'." }
    }
    $id = [Guid]::NewGuid().ToString('N')
    $script:PowerOperations[$id] = [pscustomobject]@{
        id = $id
        action = $Action
        vm = $Vm.Name
        job = $job
        started_at = (Get-Date).ToString('o')
    }
    # Bounded history: drop the oldest finished entries beyond the last 50 operations.
    if ($script:PowerOperations.Count -gt 50) {
        $finished = @($script:PowerOperations.Values | Where-Object {
            [string]$_.job.State -in @('Completed', 'Failed', 'Stopped')
        } | Sort-Object -Property started_at)
        $excess = $script:PowerOperations.Count - 50
        foreach ($entry in ($finished | Select-Object -First $excess)) {
            $script:PowerOperations.Remove($entry.id)
        }
    }
    return [pscustomobject]@{
        accepted = $true
        operation_id = $id
        action = $Action
        vm = $Vm.Name
        state = 'running'
        started_at = $script:PowerOperations[$id].started_at
    }
}

function Get-BackgroundOperationStatus {
    param([Parameter(Mandatory)][string]$OperationId)
    $entry = $script:PowerOperations[$OperationId]
    if ($null -eq $entry) {
        return [pscustomobject]@{
            operation_id = $OperationId; state = 'unknown'; action = $null; vm = $null
            succeeded = $null; error = $null; started_at = $null
        }
    }
    $jobState = [string]$entry.job.State
    $succeeded = $null
    $errorMessage = $null
    switch ($jobState) {
        'Completed' { $succeeded = $true }
        { $_ -in @('Failed', 'Stopped') } {
            $succeeded = $false
            $reasons = @(
                $entry.job.JobStateInfo.Reason
                $entry.job.ChildJobs | ForEach-Object { $_.JobStateInfo.Reason }
            ) | Where-Object { $null -ne $_ }
            if ($reasons.Count -gt 0) {
                $errorMessage = ($reasons | ForEach-Object { $_.Message } | Select-Object -First 3) -join ' | '
            }
        }
    }
    return [pscustomobject]@{
        operation_id = $entry.id
        state = $jobState.ToLowerInvariant()
        action = $entry.action
        vm = $entry.vm
        succeeded = $succeeded
        error = $errorMessage
        started_at = $entry.started_at
    }
}

function Invoke-BridgeOperation {
    param([Parameter(Mandatory)][string]$Operation, [Parameter(Mandatory)]$Request)
    Import-Module Hyper-V -ErrorAction Stop

    switch ($Operation) {
        'host_status' {
            $hostInfo = Get-VMHost
            $computer = Get-CimInstance -ClassName 'Win32_ComputerSystem'
            $services = @(
                Get-CimInstance -ClassName 'Win32_Service' -Filter "Name = 'vmms' OR Name = 'vmcompute'" |
                    ForEach-Object {
                        [ordered]@{ name = $_.Name; state = $_.State; start_mode = $_.StartMode }
                    }
            )
            $result = [ordered]@{
                computer_name = $hostInfo.Name
                hypervisor_present = [bool]$computer.HypervisorPresent
                logical_processor_count = [int]$hostInfo.LogicalProcessorCount
                memory_capacity_bytes = [Int64]$hostInfo.MemoryCapacity
                services = $services
            }
        }
        'vm_list' {
            $vms = @(Get-VM | ForEach-Object { Convert-Vm -Vm $_ })
            $result = @{ vms = $vms; count = $vms.Count }
        }
        'vm_get' {
            $vm = Get-ExactVm -Name $request.vm
            $result = @{ vm = (Convert-Vm -Vm $vm) }
        }
        'vm_power' {
            $vm = Get-ExactVm -Name $request.vm
            $action = [string]$request.action
            # Graceful shutdown/save can outlive the bridge timeout, so they are handed to a
            # background Hyper-V job and tracked through operation_status.
            if ($action -in @('shutdown', 'save')) {
                $accepted = Start-BackgroundVmPower -Vm $vm -Action $action
                $result = @{
                    accepted = $true
                    operation_id = $accepted.operation_id
                    action = $action
                    state = 'running'
                    started_at = $accepted.started_at
                    vm = (Convert-Vm -Vm $vm)
                }
            } else {
                switch ($action) {
                    'start' { Start-VM -VM $vm -Confirm:$false | Out-Null }
                    'pause' { Suspend-VM -VM $vm -Confirm:$false | Out-Null }
                    'resume' { Resume-VM -VM $vm -Confirm:$false | Out-Null }
                    'turn_off' { Stop-VM -VM $vm -TurnOff -Force -Confirm:$false | Out-Null }
                    'reset' { Restart-VM -VM $vm -Force -Confirm:$false | Out-Null }
                    default { throw 'Unsupported power action.' }
                }
                $result = @{
                    accepted = $false
                    operation_id = $null
                    action = $action
                    state = 'completed'
                    started_at = $null
                    vm = (Convert-Vm -Vm (Get-ExactVm -Name $request.vm))
                }
            }
        }
        'operation_status' {
            $status = Get-BackgroundOperationStatus -OperationId ([string]$request.operation_id)
            $result = @{
                operation_id = $status.operation_id
                state = $status.state
                action = $status.action
                vm = $status.vm
                succeeded = $status.succeeded
                error = $status.error
                started_at = $status.started_at
            }
        }
        'checkpoint_list' {
            $vm = Get-ExactVm -Name $request.vm
            $checkpoints = @(Get-VMSnapshot -VM $vm | ForEach-Object { Convert-Checkpoint -Snapshot $_ })
            $result = @{ vm = $vm.Name; checkpoints = $checkpoints; count = $checkpoints.Count }
        }
        'checkpoint_create' {
            $vm = Get-ExactVm -Name $request.vm
            $snapshot = Checkpoint-VM -VM $vm -SnapshotName ([string]$request.name) -Passthru -Confirm:$false
            $result = @{ checkpoint = (Convert-Checkpoint -Snapshot $snapshot) }
        }
        'checkpoint_restore' {
            $vm = Get-ExactVm -Name $request.vm
            $snapshot = Get-ExactCheckpoint -Vm $vm -SnapshotId $request.snapshot_id
            Restore-VMSnapshot -VMSnapshot $snapshot -Confirm:$false | Out-Null
            $result = @{ restored = $true; checkpoint = (Convert-Checkpoint -Snapshot $snapshot) }
        }
        'checkpoint_delete' {
            $vm = Get-ExactVm -Name $request.vm
            $snapshot = Get-ExactCheckpoint -Vm $vm -SnapshotId $request.snapshot_id
            $converted = Convert-Checkpoint -Snapshot $snapshot
            Remove-VMSnapshot -VMSnapshot $snapshot -Confirm:$false | Out-Null
            $result = @{ deleted = $true; checkpoint = $converted }
        }
        'guest_status' {
            $vm = Get-ExactVm -Name $request.vm
            $status = Invoke-GuestQuery -Vm $vm -Request $request -Query {
                $os = Get-CimInstance -ClassName 'Win32_OperatingSystem'
                $computer = Get-CimInstance -ClassName 'Win32_ComputerSystem'
                $kd = (& bcdedit.exe /enum '{dbgsettings}' 2>&1 | Out-String)
                [pscustomobject]@{
                    computer_name = $env:COMPUTERNAME
                    windows_caption = $os.Caption
                    windows_version = $os.Version
                    last_boot_time = $os.LastBootUpTime.ToString('o')
                    free_system_drive_bytes = [Int64](Get-PSDrive -Name $env:SystemDrive.TrimEnd(':')).Free
                    memory_total_bytes = [Int64]$computer.TotalPhysicalMemory
                    memory_free_bytes = [Int64]$os.FreePhysicalMemory * 1KB
                    kernel_debug_settings = $kd
                }
            }
            $result = @{ vm = $vm.Name; guest = $status }
        }
        'guest_processes' {
            $vm = Get-ExactVm -Name $request.vm
            $filter = [string]$request.name_contains
            $processes = Invoke-GuestQuery -Vm $vm -Request $request -Arguments $filter -Query {
                param($Filter)
                Get-Process | Where-Object { -not $Filter -or $_.ProcessName -like "*$Filter*" } |
                    Sort-Object -Property CPU -Descending | Select-Object -First 200 |
                    ForEach-Object {
                        [pscustomobject]@{
                            id = $_.Id; name = $_.ProcessName
                            cpu_seconds = if ($null -eq $_.CPU) { $null } else { [Math]::Round($_.CPU, 3) }
                            working_set_bytes = [Int64]$_.WorkingSet64
                        }
                    }
            }
            $result = @{ vm = $vm.Name; processes = @($processes) }
        }
        'guest_services' {
            $vm = Get-ExactVm -Name $request.vm
            $filter = [string]$request.name_contains
            $services = Invoke-GuestQuery -Vm $vm -Request $request -Arguments $filter, ([bool]$request.include_stopped) -Query {
                param($Filter, $IncludeStopped)
                Get-CimInstance -ClassName 'Win32_Service' |
                    Where-Object {
                        (-not $Filter -or $_.Name -like "*$Filter*" -or $_.DisplayName -like "*$Filter*") -and
                        ($IncludeStopped -or $_.State -eq 'Running')
                    } | Sort-Object Name | Select-Object -First 200 |
                    ForEach-Object {
                        [pscustomobject]@{ name = $_.Name; display_name = $_.DisplayName; state = $_.State; start_mode = $_.StartMode }
                    }
            }
            $result = @{ vm = $vm.Name; services = @($services) }
        }
        'guest_events' {
            $vm = Get-ExactVm -Name $request.vm
            $eventId = if ($request.PSObject.Properties.Name -contains 'event_id') { [int]$request.event_id } else { $null }
            $eventLevel = if ($request.PSObject.Properties.Name -contains 'level') { [int]$request.level } else { $null }
            $events = Invoke-GuestQuery -Vm $vm -Request $request -Arguments @(
                [string]$request.log_name, [int]$request.max_events, $eventId, $eventLevel, [int]$request.hours
            ) -Query {
                param($LogName, $MaximumEvents, $EventId, $Level, $Hours)
                $filter = @{ LogName = $LogName; StartTime = (Get-Date).AddHours(-$Hours) }
                if ($null -ne $EventId) { $filter['Id'] = [int]$EventId }
                if ($null -ne $Level) { $filter['Level'] = [int]$Level }
                try {
                    Get-WinEvent -FilterHashtable $filter -MaxEvents $MaximumEvents -ErrorAction Stop |
                        ForEach-Object {
                            [pscustomobject]@{
                                record_id = $_.RecordId; time = $_.TimeCreated.ToString('o')
                                id = $_.Id; level = [int]$_.Level; provider = $_.ProviderName
                                message = $_.Message
                            }
                        }
                } catch {
                    if ($_.FullyQualifiedErrorId -notmatch 'NoMatchingEventsFound') { throw }
                }
            }
            $result = @{ vm = $vm.Name; events = @($events) }
        }
        'guest_exec' {
            $vm = Get-ExactVm -Name $request.vm
            $encodedCommand = Convert-ToBase64Text -Text ([string]$request.command)
            $guestResult = Invoke-GuestQuery -Vm $vm -Request $request `
                -Arguments $encodedCommand, ([int]$request.max_output_chars) -Query {
                    param($Encoded, $MaximumCharacters)
                    $command = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Encoded))
                    $output = ''
                    $errorMessage = ''
                    $success = $true
                    $exitCode = 0
                    $errorRecords = @()
                    $hadErrors = $false
                    # Non-terminating cmdlet errors never change $LASTEXITCODE, so the only
                    # reliable signal is the runspace error collection before/after.
                    $errorCountBefore = $Error.Count
                    $previousPreference = $ErrorActionPreference
                    try {
                        # Native stderr must not become a terminating NativeCommandError on PS 5.1.
                        $ErrorActionPreference = 'Continue'
                        $global:LASTEXITCODE = $null
                        $output = (& ([ScriptBlock]::Create($command)) 2>&1 | ForEach-Object {
                            if ($_ -is [Management.Automation.ErrorRecord]) { $_.ToString() } else { $_ }
                        } | Out-String -Width 4096)
                        if ($null -ne $LASTEXITCODE) {
                            $exitCode = [int]$LASTEXITCODE
                            if ($exitCode -ne 0) { $success = $false }
                        }
                    } catch {
                        $success = $false
                        $exitCode = 1
                        $errorMessage = $_.Exception.Message
                    } finally {
                        $ErrorActionPreference = $previousPreference
                        $added = $Error.Count - $errorCountBefore
                        if ($added -gt 0) {
                            $errorRecords = @($Error[0..($added - 1)] | Where-Object {
                                # Native stderr records are output, not failures: a tool that
                                # writes progress to stderr must still be able to succeed.
                                $_.FullyQualifiedErrorId -notmatch '^NativeCommandError'
                            } | ForEach-Object {
                                $message = $_.ToString()
                                if ($message.Length -gt 2000) { $message = $message.Substring(0, 2000) }
                                $message
                            })
                        }
                        $hadErrors = $errorRecords.Count -gt 0
                        if ($hadErrors) { $success = $false }
                    }
                    $truncated = $output.Length -gt $MaximumCharacters
                    if ($truncated) { $output = $output.Substring(0, $MaximumCharacters) }
                    [pscustomobject]@{
                        success = $success
                        exit_code = $exitCode
                        output = $output
                        error = $errorMessage
                        had_errors = $hadErrors
                        error_records = $errorRecords
                        output_truncated = $truncated
                    }
                }
            $result = @{ vm = $vm.Name; execution = $guestResult }
        }
        'guest_read_file' {
            $vm = Get-ExactVm -Name $request.vm
            $encodedPath = Convert-ToBase64Text -Text ([string]$request.path)
            $guestResult = Invoke-GuestQuery -Vm $vm -Request $request `
                -Arguments $encodedPath, ([int]$request.max_bytes) -Query {
                    param($EncodedPath, $MaximumBytes)
                    $path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($EncodedPath))
                    if (-not [IO.Path]::IsPathRooted($path)) { throw 'The guest path must be absolute.' }
                    $info = [IO.FileInfo]::new($path)
                    if (-not $info.Exists) { throw "Guest file not found: $path" }
                    if ($info.Length -gt $MaximumBytes) { throw "Guest file exceeds the $MaximumBytes byte limit." }
                    $bytes = [IO.File]::ReadAllBytes($info.FullName)
                    $sha = [Security.Cryptography.SHA256]::Create()
                    try { $hash = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant() }
                    finally { $sha.Dispose() }
                    [pscustomobject]@{
                        path = $info.FullName
                        size = $bytes.Length
                        content_base64 = [Convert]::ToBase64String($bytes)
                        sha256 = $hash
                    }
                }
            $result = @{ vm = $vm.Name; file = $guestResult }
        }
        'guest_write_file' {
            $vm = Get-ExactVm -Name $request.vm
            $encodedPath = Convert-ToBase64Text -Text ([string]$request.path)
            $guestResult = Invoke-GuestQuery -Vm $vm -Request $request `
                -Arguments $encodedPath, ([string]$request.content_base64),
                    ([bool]$request.overwrite), ([bool]$request.create_parent) -Query {
                    param($EncodedPath, $Content, $Overwrite, $CreateParent)
                    $path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($EncodedPath))
                    if (-not [IO.Path]::IsPathRooted($path)) { throw 'The guest path must be absolute.' }
                    $fullPath = [IO.Path]::GetFullPath($path)
                    $directory = [IO.Path]::GetDirectoryName($fullPath)
                    if (-not [IO.Directory]::Exists($directory)) {
                        if (-not $CreateParent) { throw "Guest directory does not exist: $directory" }
                        [IO.Directory]::CreateDirectory($directory) | Out-Null
                    }
                    if ([IO.File]::Exists($fullPath) -and -not $Overwrite) {
                        throw "Guest file already exists; overwrite was not approved: $fullPath"
                    }
                    $bytes = [Convert]::FromBase64String($Content)
                    $temporary = Join-Path $directory ('.hvmcp-' + [Guid]::NewGuid().ToString('N') + '.tmp')
                    try {
                        [IO.File]::WriteAllBytes($temporary, $bytes)
                        if ([IO.File]::Exists($fullPath)) {
                            [IO.File]::Replace($temporary, $fullPath, $null)
                        } else {
                            [IO.File]::Move($temporary, $fullPath)
                        }
                    } finally {
                        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
                    }
                    $sha = [Security.Cryptography.SHA256]::Create()
                    try { $hash = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant() }
                    finally { $sha.Dispose() }
                    [pscustomobject]@{ path = $fullPath; bytes_written = $bytes.Length; sha256 = $hash }
                }
            $result = @{ vm = $vm.Name; file = $guestResult }
        }
        'guest_read_chunk' {
            $vm = Get-ExactVm -Name $request.vm
            $encodedPath = Convert-ToBase64Text -Text ([string]$request.path)
            $guestResult = Invoke-GuestQuery -Vm $vm -Request $request `
                -Arguments $encodedPath, ([Int64]$request.offset), ([int]$request.count) -Query {
                    param($EncodedPath, $Offset, $Count)
                    $path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($EncodedPath))
                    if (-not [IO.Path]::IsPathRooted($path)) { throw 'The guest path must be absolute.' }
                    $stream = [IO.File]::OpenRead($path)
                    try {
                        if ($Offset -lt 0 -or $Offset + $Count -gt $stream.Length) {
                            throw 'The requested evidence chunk is outside the file.'
                        }
                        $null = $stream.Seek($Offset, [IO.SeekOrigin]::Begin)
                        $bytes = [byte[]]::new($Count)
                        $read = 0
                        while ($read -lt $Count) {
                            $current = $stream.Read($bytes, $read, $Count - $read)
                            if ($current -eq 0) { throw 'The guest file was truncated during reading.' }
                            $read += $current
                        }
                    } finally { $stream.Dispose() }
                    $sha = [Security.Cryptography.SHA256]::Create()
                    try { $hash = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant() }
                    finally { $sha.Dispose() }
                    [pscustomobject]@{
                        offset = $Offset; size = $read
                        content_base64 = [Convert]::ToBase64String($bytes); sha256 = $hash
                    }
                }
            $result = @{ vm = $vm.Name; chunk = $guestResult }
        }
        'guest_assemble_file' {
            $vm = Get-ExactVm -Name $request.vm
            $encodedPath = Convert-ToBase64Text -Text ([string]$request.path)
            $encodedParts = Convert-ToBase64Text -Text ([string]$request.parts_directory)
            $guestResult = Invoke-GuestQuery -Vm $vm -Request $request `
                -Arguments $encodedPath, $encodedParts, ([int]$request.part_count),
                    ([Int64]$request.expected_size), ([string]$request.expected_sha256) -Query {
                    param($EncodedPath, $EncodedParts, $PartCount, $ExpectedSize, $ExpectedHash)
                    $path = [IO.Path]::GetFullPath([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($EncodedPath)))
                    $parts = [IO.Path]::GetFullPath([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($EncodedParts)))
                    $packageRoot = [IO.Path]::GetFullPath('C:\DriverLab\Packages\')
                    $transferRoot = [IO.Path]::GetFullPath('C:\DriverLab\Transfers\')
                    if (-not $path.StartsWith($packageRoot, [StringComparison]::OrdinalIgnoreCase)) {
                        throw 'Assembly destination must remain under C:\DriverLab\Packages.'
                    }
                    if (-not $parts.StartsWith($transferRoot, [StringComparison]::OrdinalIgnoreCase)) {
                        throw 'Assembly chunks must remain under C:\DriverLab\Transfers.'
                    }
                    if ([IO.File]::Exists($path)) { throw 'The staged package file already exists; it is immutable.' }
                    $directory = [IO.Path]::GetDirectoryName($path)
                    [IO.Directory]::CreateDirectory($directory) | Out-Null
                    $temporary = Join-Path $directory ('.hvmcp-' + [Guid]::NewGuid().ToString('N') + '.tmp')
                    $hash = [Security.Cryptography.SHA256]::Create()
                    $length = [Int64]0
                    try {
                        $target = [IO.FileStream]::new($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                        try {
                            for ($index = 0; $index -lt $PartCount; $index++) {
                                $part = Join-Path $parts ('part-{0:D6}' -f $index)
                                $source = [IO.FileStream]::new($part, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
                                try {
                                    $length += $source.Length
                                    if ($length -gt $ExpectedSize) { throw 'The staged chunks exceed the expected file size.' }
                                    $source.CopyTo($target)
                                } finally { $source.Dispose() }
                            }
                        } finally { $target.Dispose() }
                        $source = [IO.File]::OpenRead($temporary)
                        try { $actual = [BitConverter]::ToString($hash.ComputeHash($source)).Replace('-', '').ToLowerInvariant() }
                        finally { $source.Dispose() }
                        if ($length -ne $ExpectedSize -or $actual -ne $ExpectedHash) {
                            throw 'The assembled guest file failed its size or SHA-256 check.'
                        }
                        [IO.File]::Move($temporary, $path)
                    } finally {
                        $hash.Dispose()
                        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
                        if ([IO.Directory]::Exists($parts)) { [IO.Directory]::Delete($parts, $true) }
                    }
                    [pscustomobject]@{ path = $path; bytes_written = $length; sha256 = $actual }
                }
            $result = @{ vm = $vm.Name; file = $guestResult }
        }
        'console_info' {
            $vm = Get-ExactVm -Name $request.vm
            $vmCim = Get-VmCim -Vm $vm
            $controllers = @(Get-CimAssociatedInstance -InputObject $vmCim `
                -Association 'Msvm_SystemDevice' -ResultClassName 'Msvm_SyntheticDisplayController')
            $heads = @(
                foreach ($controller in $controllers) {
                    Get-CimAssociatedInstance -InputObject $controller `
                        -Association 'Msvm_VideoHeadOnController' -ResultClassName 'Msvm_VideoHead'
                }
            )
            $resolutions = @(
                $heads | ForEach-Object {
                    [ordered]@{
                        horizontal = [int]$_.CurrentHorizontalResolution
                        vertical = [int]$_.CurrentVerticalResolution
                    }
                }
            )
            $result = @{ vm = $vm.Name; state = $vm.State.ToString(); video_heads = $resolutions }
        }
        'console_type_text' {
            $vm = Get-ExactVm -Name $request.vm
            $keyboard = Get-InputDevice -Vm $vm -ClassName 'Msvm_Keyboard'
            $text = [string]$request.text
            $segments = [regex]::Split($text, '(\r\n|\r|\n)')
            $chunks = 0
            foreach ($segment in $segments) {
                if ($segment -match '^\r\n$|^\r$|^\n$') {
                    Invoke-InputMethod -Device $keyboard -Method 'TypeKey' -Arguments @{ KeyCode = [uint32]13 }
                    continue
                }
                for ($offset = 0; $offset -lt $segment.Length;) {
                    $length = [Math]::Min(256, $segment.Length - $offset)
                    $lastIndex = $offset + $length - 1
                    if ($length -eq 256 -and [char]::IsHighSurrogate($segment[$lastIndex])) { $length-- }
                    $chunk = $segment.Substring($offset, $length)
                    Invoke-InputMethod -Device $keyboard -Method 'TypeText' -Arguments @{ AsciiText = $chunk }
                    $chunks++
                    $offset += $length
                    Start-Sleep -Milliseconds 15
                }
            }
            $result = @{ vm = $vm.Name; chunks_sent = $chunks; completed = $true }
        }
        'console_press_keys' {
            $vm = Get-ExactVm -Name $request.vm
            $keyboard = Get-InputDevice -Vm $vm -ClassName 'Msvm_Keyboard'
            $pressed = @()
            try {
                foreach ($key in $request.keys) {
                    $code = Convert-KeyName -Name ([string]$key)
                    Invoke-InputMethod -Device $keyboard -Method 'PressKey' -Arguments @{ KeyCode = $code }
                    $pressed += $code
                }
            } finally {
                for ($index = $pressed.Count - 1; $index -ge 0; $index--) {
                    Invoke-InputMethod -Device $keyboard -Method 'ReleaseKey' -Arguments @{ KeyCode = $pressed[$index] }
                }
            }
            $result = @{ vm = $vm.Name; keys_sent = $pressed.Count; completed = $true }
        }
        'console_key_state' {
            $vm = Get-ExactVm -Name $request.vm
            $keyboard = Get-InputDevice -Vm $vm -ClassName 'Msvm_Keyboard'
            $arguments = @{ KeyCode = Convert-KeyName -Name ([string]$request.key) }
            $method = if ([bool]$request.down) { 'PressKey' } else { 'ReleaseKey' }
            Invoke-InputMethod -Device $keyboard -Method $method -Arguments $arguments
            $result = @{ vm = $vm.Name; key = [string]$request.key; down = [bool]$request.down }
        }
        'console_mouse_buttons' {
            $vm = Get-ExactVm -Name $request.vm
            $mouse = Get-InputDevice -Vm $vm -ClassName 'Msvm_SyntheticMouse'
            $states = @([bool]$request.left, [bool]$request.right, [bool]$request.middle)
            for ($index = 0; $index -lt $states.Count; $index++) {
                Invoke-InputMethod -Device $mouse -Method 'SetButtonState' -Arguments @{
                    ButtonIndex = [uint32]($index + 1); ButtonState = [uint32]$states[$index]
                }
            }
            $result = @{ vm = $vm.Name; left = $states[0]; right = $states[1]; middle = $states[2] }
        }
        'console_ctrl_alt_delete' {
            $vm = Get-ExactVm -Name $request.vm
            $keyboard = Get-InputDevice -Vm $vm -ClassName 'Msvm_Keyboard'
            Invoke-InputMethod -Device $keyboard -Method 'TypeCtrlAltDel'
            $result = @{ vm = $vm.Name; completed = $true }
        }
        'console_mouse_move' {
            $vm = Get-ExactVm -Name $request.vm
            $mouse = Get-InputDevice -Vm $vm -ClassName 'Msvm_SyntheticMouse'
            Invoke-InputMethod -Device $mouse -Method 'SetAbsolutePosition' -Arguments @{
                HorizontalPosition = [int]$request.x; VerticalPosition = [int]$request.y
            }
            $result = @{ vm = $vm.Name; x = $request.x; y = $request.y }
        }
        'console_click' {
            $vm = Get-ExactVm -Name $request.vm
            $mouse = Get-InputDevice -Vm $vm -ClassName 'Msvm_SyntheticMouse'
            if ($request.PSObject.Properties.Name -contains 'x') {
                Invoke-InputMethod -Device $mouse -Method 'SetAbsolutePosition' -Arguments @{
                    HorizontalPosition = [int]$request.x; VerticalPosition = [int]$request.y
                }
            }
            Invoke-InputMethod -Device $mouse -Method 'ClickButton' -Arguments @{
                ButtonIndex = [uint32]$request.button_index
            }
            $result = @{ vm = $vm.Name; button_index = $request.button_index; completed = $true }
        }
        'console_scroll' {
            $vm = Get-ExactVm -Name $request.vm
            $mouse = Get-InputDevice -Vm $vm -ClassName 'Msvm_SyntheticMouse'
            Invoke-InputMethod -Device $mouse -Method 'SetScrollPosition' -Arguments @{
                ScrollPositionDelta = [int]$request.delta
            }
            $result = @{ vm = $vm.Name; delta = $request.delta; completed = $true }
        }
        'media_list' {
            $vm = Get-ExactVm -Name $request.vm
            $drives = @(Get-VMDvdDrive -VM $vm | ForEach-Object { Convert-DvdDrive -Drive $_ })
            $result = @{ vm = $vm.Name; state = $vm.State.ToString(); generation = [int]$vm.Generation; dvd_drives = $drives }
        }
        'media_mount' {
            $vm = Get-ExactVm -Name $request.vm
            if ($vm.State -ne [Microsoft.HyperV.PowerShell.VMState]::Off) {
                throw 'The VM must be Off before changing installation media or boot order. The server will not stop it automatically.'
            }
            $iso = Get-Item -LiteralPath ([string]$request.path) -ErrorAction Stop
            if ($iso.PSIsContainer -or $iso.Extension -ine '.iso') { throw 'The media path must be an existing .iso file.' }
            $number = [int]$request.controller_number
            $location = [int]$request.controller_location
            $drive = Get-ExactDvdDrive -Vm $vm -ControllerNumber $number -ControllerLocation $location
            Set-VMDvdDrive -VM $vm -ControllerNumber $number -ControllerLocation $location -Path $iso.FullName -Confirm:$false -ErrorAction Stop | Out-Null
            $bootChanged = $false
            if ([bool]$request.boot_first) {
                if ([int]$vm.Generation -eq 2) {
                    Set-VMFirmware -VM $vm -FirstBootDevice $drive -Confirm:$false -ErrorAction Stop | Out-Null
                } else {
                    $bios = Get-VMBios -VM $vm
                    $order = @('CD') + @($bios.StartupOrder | Where-Object { $_ -ne 'CD' })
                    Set-VMBios -VM $vm -StartupOrder $order -Confirm:$false -ErrorAction Stop | Out-Null
                }
                $bootChanged = $true
            }
            $result = @{
                vm = $vm.Name; drive = Convert-DvdDrive -Drive (Get-ExactDvdDrive -Vm $vm -ControllerNumber $number -ControllerLocation $location)
                boot_first = $bootChanged; completed = $true
            }
        }
        'media_eject' {
            $vm = Get-ExactVm -Name $request.vm
            if ($vm.State -ne [Microsoft.HyperV.PowerShell.VMState]::Off) {
                throw 'The VM must be Off before ejecting installation media. The server will not stop it automatically.'
            }
            $number = [int]$request.controller_number
            $location = [int]$request.controller_location
            $null = Get-ExactDvdDrive -Vm $vm -ControllerNumber $number -ControllerLocation $location
            Set-VMDvdDrive -VM $vm -ControllerNumber $number -ControllerLocation $location -Path $null -Confirm:$false -ErrorAction Stop | Out-Null
            $result = @{
                vm = $vm.Name; drive = Convert-DvdDrive -Drive (Get-ExactDvdDrive -Vm $vm -ControllerNumber $number -ControllerLocation $location)
                completed = $true
            }
        }
        'debug_serial_status' {
            $vm = Get-ExactVm -Name $request.vm
            $ports = @(
                Get-VMComPort -VM $vm | ForEach-Object { Convert-ComPort -Port $_ }
            )
            $tools = [ordered]@{
                windbgx = [bool](Get-Command 'WinDbgX.exe' -ErrorAction SilentlyContinue)
                kdnet = 'C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\kdnet.exe'
                kdnet_installed = Test-Path -LiteralPath 'C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\kdnet.exe' -PathType Leaf
            }
            $result = @{ vm = $vm.Name; state = $vm.State.ToString(); com_ports = $ports; tools = $tools }
        }
        'debug_serial_prepare' {
            $vm = Get-ExactVm -Name $request.vm
            if ($vm.State -ne [Microsoft.HyperV.PowerShell.VMState]::Off) {
                throw 'The VM must be Off before configuring COM1. The server will not stop it automatically.'
            }
            $pipeName = [string]$request.pipe_name
            if ($pipeName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') {
                throw 'The named pipe must contain only letters, digits, dot, underscore, or hyphen.'
            }
            $pipePath = '\\.\pipe\' + $pipeName
            $port = Set-VMComPort -VM $vm -Number 1 -Path $pipePath -DebuggerMode On -Passthru -Confirm:$false
            $result = @{
                vm = $vm.Name
                port_number = 1
                pipe_path = $port.Path
                debugger_mode = $port.DebuggerMode.ToString()
                baud_rate = 115200
                windbg_arguments = "-k com:pipe,port=$pipePath,baud=115200,reconnect,resets=0"
                next_steps = @(
                    'Configure guest BCD debugging separately while the VM is Running, if it is not already enabled.'
                    'Start WinDbg with the returned arguments before starting the VM to catch early boot.'
                    'Do not enable Enhanced Session mode while stopped at a kernel breakpoint.'
                    'Checkpoint before driver testing; kernel debugging does not prevent a bugcheck.'
                )
            }
        }
        'debug_guest_prepare' {
            $vm = Get-ExactVm -Name $request.vm
            if ($vm.State -ne [Microsoft.HyperV.PowerShell.VMState]::Running) {
                throw 'The VM must be Running for PowerShell Direct guest configuration. The server will not start it automatically.'
            }
            $guestResult = Invoke-GuestQuery -Vm $vm -Request $request -Query {
                $ErrorActionPreference = 'Stop'
                $secureBootSupported = $true
                try {
                    $secureBootEnabled = [bool](Confirm-SecureBootUEFI -ErrorAction Stop)
                } catch {
                    if ($_.FullyQualifiedErrorId -match 'NotSupported|SecureBootUEFINotSupported') {
                        $secureBootSupported = $false
                        $secureBootEnabled = $false
                    } else {
                        throw "Secure Boot preflight failed; BCD was not changed: $($_.Exception.Message)"
                    }
                }
                if ($secureBootEnabled) {
                    throw 'Secure Boot is enabled. BCD was not changed; disable Secure Boot deliberately while the VM is Off, then retry.'
                }
                $dbg = (& bcdedit.exe /dbgsettings SERIAL DEBUGPORT:1 BAUDRATE:115200 2>&1 | Out-String)
                if ($LASTEXITCODE -ne 0) { throw "bcdedit /dbgsettings failed: $dbg" }
                $debug = (& bcdedit.exe /debug '{current}' on 2>&1 | Out-String)
                if ($LASTEXITCODE -ne 0) { throw "bcdedit /debug failed: $debug" }
                [pscustomobject]@{
                    secure_boot_supported = $secureBootSupported
                    secure_boot_enabled = $secureBootEnabled
                    debug_settings = $dbg; enable_result = $debug
                }
            }
            $result = @{
                vm = $vm.Name; guest = $guestResult; guest_debug_enabled = $true
                reboot_required = $true; automatic_reboot = $false
            }
        }
        default {
            throw ([System.ArgumentException]::new("Unsupported bridge operation '$Operation'."))
        }
    }
    if ($result -isnot [System.Collections.IDictionary]) {
        throw "Bridge operation '$Operation' returned an invalid result."
    }
    return $result
}

function Convert-ComPort {
    param([Parameter(Mandatory)]$Port)
    $name = [string]$Port.Name
    if ($name -notmatch '^COM\s+([12])$') {
        throw "Could not determine the COM port number from '$name'."
    }
    return [ordered]@{
        number = [int]$Matches[1]
        path = $Port.Path
        debugger_mode = $Port.DebuggerMode.ToString()
    }
}

function Get-ExactDvdDrive {
    param(
        [Parameter(Mandatory)]$Vm,
        [Parameter(Mandatory)][int]$ControllerNumber,
        [Parameter(Mandatory)][int]$ControllerLocation
    )
    $drives = @(
        Get-VMDvdDrive -VM $Vm | Where-Object {
            $_.ControllerNumber -eq $ControllerNumber -and $_.ControllerLocation -eq $ControllerLocation
        }
    )
    if ($drives.Count -ne 1) {
        throw "Expected one DVD drive at controller $ControllerNumber location $ControllerLocation; found $($drives.Count). The server does not create drives implicitly."
    }
    return $drives[0]
}

function Convert-DvdDrive {
    param([Parameter(Mandatory)]$Drive)
    return [ordered]@{
        controller_number = [int]$Drive.ControllerNumber
        controller_location = [int]$Drive.ControllerLocation
        path = $Drive.Path
        mounted = -not [string]::IsNullOrWhiteSpace([string]$Drive.Path)
    }
}

function Invoke-BridgeRequest {
    param([Parameter(Mandatory)][string]$Operation, [Parameter(Mandatory)]$Request)
    try {
        if ($Request.version -ne 1) { throw 'Unsupported bridge request version.' }
        $result = Invoke-BridgeOperation -Operation $Operation -Request $Request
        return @{ ok = $true; result = $result }
    } catch {
        $errorRecord = $_
        $message = $errorRecord.Exception.Message
        $category = [string]$errorRecord.CategoryInfo.Category
        $code = 'bridge_error'
        if ($category -eq 'PermissionDenied' -or $errorRecord.Exception.HResult -eq -2147024891) {
            $code = 'access_denied'
        } elseif ($errorRecord.Exception -is [System.ArgumentException] -or $category -eq 'InvalidArgument') {
            $code = 'invalid_argument'
        } elseif ($category -eq 'ObjectNotFound') {
            $code = 'not_found'
        } elseif ($message -match 'permission|permiss|access denied|acesso negado') {
            # Some Hyper-V cmdlets wrap access failures in a generic RuntimeException.
            $code = 'access_denied'
        }
        if ($code -eq 'access_denied') {
            $message = "Hyper-V access denied. Run the MCP host elevated or grant Hyper-V Administrators access. Original error: $message"
        }
        return @{ ok = $false; error = @{ code = $code; message = $message } }
    }
}

if ($Server) {
    while ($null -ne ($line = [Console]::In.ReadLine())) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        try {
            $request = $line | ConvertFrom-Json
            if ([string]::IsNullOrWhiteSpace([string]$request.operation)) {
                throw 'The request is missing its operation.'
            }
            $response = Invoke-BridgeRequest -Operation ([string]$request.operation) -Request $request
        } catch {
            $response = @{ ok = $false; error = @{ code = 'invalid_request'; message = $_.Exception.Message } }
        }
        Write-Response -Response $response
        [Console]::Out.Flush()
    }
    exit 0
}

if ([string]::IsNullOrWhiteSpace($Operation)) { throw 'Specify -Operation or -Server.' }
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$response = Invoke-BridgeRequest -Operation $Operation -Request $request
Write-Response -Response $response
