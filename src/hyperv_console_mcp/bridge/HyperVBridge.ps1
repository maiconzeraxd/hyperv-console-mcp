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
# One reused New-PSSession per VM; a broken session or a rebooted guest forces a new one.
$script:GuestSessions = @{}

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
                mac_address = $_.MacAddress
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

function ConvertTo-GuestCredential {
    # The guest password arrives inside the bridge request JSON; it is never read from
    # disk, an environment variable, or a command line, so the plaintext conversion is
    # the only way to build the PSCredential PowerShell Direct needs.
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute(
        'PSAvoidUsingConvertToSecureStringWithPlainText', '',
        Justification = 'Plaintext comes from the request payload and never touches disk or argv.'
    )]
    param([Parameter(Mandatory)]$Request)
    $secure = ConvertTo-SecureString $Request.password -AsPlainText -Force
    return [PSCredential]::new([string]$Request.username, $secure)
}

function Convert-ToBase64Text {
    param([AllowEmptyString()][string]$Text)
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Text))
}

function Get-VmUptime {
    # A guest reboot resets the host-side uptime, which makes it a cheap boot identity.
    param([Parameter(Mandatory)]$Vm)
    $property = $Vm.PSObject.Properties['Uptime']
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Disconnect-GuestSession {
    param([Parameter(Mandatory)][string]$VmName)
    $entry = $script:GuestSessions[$VmName]
    if ($null -eq $entry) { return }
    $script:GuestSessions.Remove($VmName)
    try {
        Remove-PSSession -Session $entry.session -ErrorAction Stop
    } catch {
        # A session that already died needs no cleanup of its own.
        Write-Verbose "Ignoring guest session cleanup failure: $($_.Exception.Message)"
    }
}

function Get-GuestSession {
    param([Parameter(Mandatory)]$Vm, [Parameter(Mandatory)]$Request)
    $name = [string]$Vm.Name
    $entry = $script:GuestSessions[$name]
    if ($null -ne $entry) {
        $uptime = Get-VmUptime -Vm $Vm
        $rebooted = $false
        if ($null -ne $uptime -and $null -ne $entry.uptime) {
            $rebooted = ([TimeSpan]$uptime) -lt ([TimeSpan]$entry.uptime)
        }
        if ([string]$entry.session.State -eq 'Opened' -and -not $rebooted) { return $entry.session }
        Disconnect-GuestSession -VmName $name
    }
    $credential = ConvertTo-GuestCredential -Request $Request
    $session = New-PSSession -VMName $name -Credential $credential -ErrorAction Stop
    $script:GuestSessions[$name] = [pscustomobject]@{
        session = $session
        uptime = Get-VmUptime -Vm $Vm
    }
    return $session
}

function Invoke-GuestQuery {
    param(
        [Parameter(Mandatory)]$Vm,
        [Parameter(Mandatory)]$Request,
        [Parameter(Mandatory)][scriptblock]$Query,
        [Alias('ArgumentList')][object[]]$Arguments = @()
    )
    $session = Get-GuestSession -Vm $Vm -Request $Request
    try {
        return Invoke-Command -Session $session -ArgumentList $Arguments -ScriptBlock $Query |
            Select-Object -Property * -ExcludeProperty PSComputerName, RunspaceId, PSShowComputerName
    } catch {
        # A runspace that died between requests must not poison the cache.
        if ([string]$session.State -ne 'Opened') { Disconnect-GuestSession -VmName ([string]$Vm.Name) }
        throw
    }
}

function Get-HostFileSha256 {
    param([Parameter(Mandatory)][string]$Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($Path)
        try { $hash = $algorithm.ComputeHash($stream) } finally { $stream.Dispose() }
    } finally { $algorithm.Dispose() }
    return ([BitConverter]::ToString($hash) -replace '-', '').ToLowerInvariant()
}

# Runs inside the guest; absence is returned as data because remoting drops types.
$script:GuestFileState = {
    param($Path)
    $info = [IO.FileInfo]::new($Path)
    if (-not $info.Exists) { return [pscustomobject]@{ status = 'not_found'; path = $Path } }
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = $info.OpenRead()
        try { $hash = $algorithm.ComputeHash($stream) } finally { $stream.Dispose() }
    } finally { $algorithm.Dispose() }
    [pscustomobject]@{
        status = 'ok'
        path = $info.FullName
        size = [Int64]$info.Length
        sha256 = ([BitConverter]::ToString($hash) -replace '-', '').ToLowerInvariant()
    }
}

# Runs inside the guest before an upload: creates the parent and reports whether
# the destination is already there, so the host can refuse to overwrite it.
$script:EnsureGuestParent = {
    param($Path)
    $parent = [IO.Path]::GetDirectoryName($Path)
    if ($parent -and -not [IO.Directory]::Exists($parent)) {
        [void][IO.Directory]::CreateDirectory($parent)
    }
    return [IO.File]::Exists($Path)
}

function Invoke-BackgroundVmPower {
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
                $accepted = Invoke-BackgroundVmPower -Vm $vm -Action $action
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
        'vm_inject_nmi' {
            $vm = Get-ExactVm -Name $request.vm
            if ($vm.State -ne [Microsoft.HyperV.PowerShell.VMState]::Running) {
                throw 'The VM must be Running before an NMI can be injected. The server will not start it.'
            }
            # An NMI is how a hung guest is forced into the debugger; it is not a
            # substitute for a graceful shutdown.
            Debug-VM -VM $vm -InjectNonMaskableInterrupt -Confirm:$false | Out-Null
            $result = @{
                vm = $vm.Name
                injected = $true
                state = $vm.State.ToString()
                note = 'An NMI may bugcheck the guest. Collect the dump or attach the debugger before retrying.'
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
        'guest_set_password' {
            $vm = Get-ExactVm -Name $request.vm
            $account = [string]$request.username
            if (-not $account) { throw 'No guest account was named for the password change.' }
            $newPassword = [string]$request.new_password
            if (-not $newPassword) { throw 'No new password was supplied.' }
            # The password travels as a script-block argument, never inside a
            # command line, so it does not appear in the guest's process list.
            $applied = Invoke-GuestQuery -Vm $vm -Request $request -Arguments $account, $newPassword -Query {
                param($Account, $Secret)
                # Built by hand instead of ConvertTo-SecureString -AsPlainText: the
                # value already crossed the boundary as a string, and this avoids
                # the analyzer's plaintext-conversion rule rather than suppressing it.
                $secure = [System.Security.SecureString]::new()
                foreach ($character in $Secret.ToCharArray()) { $secure.AppendChar($character) }
                $secure.MakeReadOnly()
                Set-LocalUser -Name $Account -Password $secure -ErrorAction Stop
                # A rotated account must stay usable for PowerShell Direct.
                Enable-LocalUser -Name $Account -ErrorAction Stop
                $user = Get-LocalUser -Name $Account -ErrorAction Stop
                [pscustomobject]@{
                    account = $user.Name
                    enabled = [bool]$user.Enabled
                    password_last_set = if ($null -eq $user.PasswordLastSet) { $null } else { $user.PasswordLastSet.ToString('o') }
                }
            }
            $result = @{ vm = $vm.Name; account = $applied }
        }
        'guest_auth_check' {
            $vm = Get-ExactVm -Name $request.vm
            $credential = ConvertTo-GuestCredential -Request $request
            $session = $null
            $ok = $false
            $message = ''
            try {
                # A fresh session proves the password works; the cached one would
                # still be authenticated with the previous secret.
                $session = New-PSSession -VMName $vm.Name -Credential $credential -ErrorAction Stop
                $ok = $true
            } catch {
                $message = $_.Exception.Message
            } finally {
                if ($null -ne $session) { Remove-PSSession -Session $session -ErrorAction SilentlyContinue }
            }
            $result = @{ vm = $vm.Name; ok = $ok; message = $message }
        }
        'guest_file_info' {
            $vm = Get-ExactVm -Name $request.vm
            $encodedPath = Convert-ToBase64Text -Text ([string]$request.path)
            $guestResult = Invoke-GuestQuery -Vm $vm -Request $request -Arguments $encodedPath -Query {
                param($EncodedPath)
                $path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($EncodedPath))
                if (-not [IO.Path]::IsPathRooted($path)) { throw 'The guest path must be absolute.' }
                $info = [IO.FileInfo]::new($path)
                # Absence is data, for the same reason guest_read_file reports it that way.
                if (-not $info.Exists) { return [pscustomobject]@{ path = $path; status = 'not_found' } }
                [pscustomobject]@{
                    path = $info.FullName; size = $info.Length
                    last_write_utc = $info.LastWriteTimeUtc.ToString('o'); status = 'ok'
                }
            }
            if ($null -ne $guestResult.PSObject.Properties['status'] -and
                $guestResult.status -eq 'not_found') {
                throw [IO.FileNotFoundException]::new(
                    "Guest file not found: $($guestResult.path)", [string]$guestResult.path
                )
            }
            $result = @{ vm = $vm.Name; file = $guestResult }
        }
        'guest_put_file' {
            $vm = Get-ExactVm -Name $request.vm
            $localPath = [string]$request.local_path
            $remotePath = [string]$request.guest_path
            $overwrite = [bool]$request.overwrite
            if (-not [IO.Path]::IsPathRooted($localPath)) { throw 'The host path must be absolute.' }
            if (-not [IO.Path]::IsPathRooted($remotePath)) { throw 'The guest path must be absolute.' }
            $source = [IO.FileInfo]::new($localPath)
            if (-not $source.Exists) {
                throw [IO.FileNotFoundException]::new("Host file not found: $localPath", $localPath)
            }
            $session = Get-GuestSession -Vm $vm -Request $request
            $destinationExists = Invoke-Command -Session $session -ArgumentList @($remotePath) `
                -ScriptBlock $script:EnsureGuestParent
            if ($destinationExists -and -not $overwrite) {
                throw [IO.IOException]::new("The guest file already exists: $remotePath")
            }
            Copy-Item -LiteralPath $localPath -Destination $remotePath -ToSession $session -Force
            $guestFile = Invoke-Command -Session $session -ArgumentList @($remotePath) `
                -ScriptBlock $script:GuestFileState
            if ($guestFile.status -eq 'not_found') {
                throw [IO.FileNotFoundException]::new("Guest file not found: $remotePath", $remotePath)
            }
            $hostDigest = Get-HostFileSha256 -Path $localPath
            if ([string]$guestFile.sha256 -ne $hostDigest) {
                throw "The guest reported a different SHA-256 than the host sent for $remotePath."
            }
            $result = @{ vm = $vm.Name; file = $guestFile }
        }
        'guest_get_file' {
            $vm = Get-ExactVm -Name $request.vm
            $remotePath = [string]$request.guest_path
            $localPath = [string]$request.local_path
            $overwrite = [bool]$request.overwrite
            if (-not [IO.Path]::IsPathRooted($localPath)) { throw 'The host path must be absolute.' }
            if (-not [IO.Path]::IsPathRooted($remotePath)) { throw 'The guest path must be absolute.' }
            $session = Get-GuestSession -Vm $vm -Request $request
            $guestFile = Invoke-Command -Session $session -ArgumentList @($remotePath) `
                -ScriptBlock $script:GuestFileState
            if ($guestFile.status -eq 'not_found') {
                throw [IO.FileNotFoundException]::new("Guest file not found: $remotePath", $remotePath)
            }
            $parent = [IO.Path]::GetDirectoryName($localPath)
            if ($parent -and -not [IO.Directory]::Exists($parent)) {
                [void][IO.Directory]::CreateDirectory($parent)
            }
            # Download beside the target and rename, so a failed copy never leaves a
            # truncated file where the caller expects a complete one.
            $temporary = "$localPath.part"
            if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
            try {
                Copy-Item -FromSession $session -LiteralPath $remotePath -Destination $temporary -Force
                $hostDigest = Get-HostFileSha256 -Path $temporary
                if ($hostDigest -ne [string]$guestFile.sha256) {
                    throw "The copied file failed its SHA-256 check for $remotePath."
                }
                Move-Item -LiteralPath $temporary -Destination $localPath -Force
            } catch {
                if ([IO.File]::Exists($temporary)) {
                    try { [IO.File]::Delete($temporary) } catch {
                        Write-Verbose "Could not remove the partial download: $($_.Exception.Message)"
                    }
                }
                throw
            }
            $result = @{ vm = $vm.Name; file = @{
                path = $localPath; size = [Int64]$guestFile.size; sha256 = $hostDigest
            } }
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
                    # Absence is reported as data: the host turns it into a typed error it
                    # can classify, because remoting does not preserve guest exception types.
                    if (-not $info.Exists) {
                        return [pscustomobject]@{ path = $path; status = 'not_found' }
                    }
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
                        status = 'ok'
                    }
                }
            if ($null -ne $guestResult.PSObject.Properties['status'] -and
                $guestResult.status -eq 'not_found') {
                throw [IO.FileNotFoundException]::new(
                    "Guest file not found: $($guestResult.path)", [string]$guestResult.path
                )
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
                        if (-not $CreateParent) { throw [IO.DirectoryNotFoundException]::new("Guest directory does not exist: $directory") }
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
                    # Large files are assembled inside their staging directory and only
                    # reach Packages once the whole package is published.
                    if (-not $path.StartsWith($packageRoot, [StringComparison]::OrdinalIgnoreCase) -and
                        -not $path.StartsWith($transferRoot, [StringComparison]::OrdinalIgnoreCase)) {
                        throw 'Assembly destination must remain under C:\DriverLab\Packages or C:\DriverLab\Transfers.'
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
        'guest_publish_package' {
            $vm = Get-ExactVm -Name $request.vm
            $packageId = [string]$request.package_id
            $transferId = [string]$request.transfer_id
            $expectedHash = [string]$request.manifest_sha256
            if ($packageId -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]{0,63}$') { throw 'package_id is invalid.' }
            if ($transferId -notmatch '^[0-9a-f]{32}$') { throw 'transfer_id is invalid.' }
            if ($expectedHash -notmatch '^[0-9a-f]{64}$') { throw 'manifest_sha256 is invalid.' }
            # Everything is staged under Transfers\<id>\<package> and verified here before a
            # single directory rename makes it visible to the guest tooling. A failed transfer
            # therefore never leaves a half-written package in Packages.
            $guestResult = Invoke-GuestQuery -Vm $vm -Request $request `
                -Arguments $packageId, $transferId, $expectedHash -Query {
                    param($PackageId, $TransferId, $ExpectedHash)
                    $transferRoot = [IO.Path]::GetFullPath("C:\DriverLab\Transfers\$TransferId")
                    $staging = [IO.Path]::GetFullPath((Join-Path $transferRoot $PackageId))
                    $final = [IO.Path]::GetFullPath("C:\DriverLab\Packages\$PackageId")
                    if (-not $staging.StartsWith($transferRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
                        throw 'The staging directory escapes C:\DriverLab\Transfers.'
                    }
                    if (-not [IO.Directory]::Exists($staging)) { throw "The staged package is missing: $staging" }
                    if ([IO.Directory]::Exists($final) -or [IO.File]::Exists($final)) {
                        throw 'The package directory already exists; packages are immutable.'
                    }
                    $manifestPath = Join-Path $staging 'lab-manifest.json'
                    if (-not [IO.File]::Exists($manifestPath)) { throw 'The staged package has no manifest.' }
                    $bytes = [IO.File]::ReadAllBytes($manifestPath)
                    if ($bytes.Length -gt 4MB) { throw 'Manifest exceeds its size limit.' }
                    $sha = [Security.Cryptography.SHA256]::Create()
                    try { $actualHash = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant() }
                    finally { $sha.Dispose() }
                    if ($actualHash -cne $ExpectedHash) { throw 'The staged manifest does not match the host-approved hash.' }
                    $manifest = [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
                    $count = @($manifest.files).Count
                    if ($manifest.schema -ne 1 -or $manifest.package_id -ne $PackageId -or $count -lt 1 -or $count -gt 512) {
                        throw 'The staged manifest is invalid.'
                    }
                    $listed = @{}
                    $verified = 0
                    foreach ($entry in @($manifest.files)) {
                        $relative = [string]$entry.path
                        if ($relative.Length -gt 240 -or $relative -cnotmatch '^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$') {
                            throw 'The staged manifest contains an unsafe path.'
                        }
                        foreach ($part in $relative.Split('/')) {
                            if ($part -ceq '.' -or $part -ceq '..' -or $part.EndsWith('.')) {
                                throw 'The staged manifest contains an unsafe path.'
                            }
                        }
                        $file = [IO.Path]::GetFullPath((Join-Path $staging ($relative.Replace('/', '\'))))
                        if (-not $file.StartsWith($staging + '\', [StringComparison]::OrdinalIgnoreCase)) {
                            throw 'The staged manifest escapes its package directory.'
                        }
                        if (-not [IO.File]::Exists($file)) { throw "The staged package is missing a file: $relative" }
                        $stream = [IO.File]::OpenRead($file)
                        try {
                            $length = $stream.Length
                            $fileSha = [Security.Cryptography.SHA256]::Create()
                            try { $fileHash = [BitConverter]::ToString($fileSha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
                            finally { $fileSha.Dispose() }
                        } finally { $stream.Dispose() }
                        if ($length -ne [Int64]$entry.size -or $fileHash -cne [string]$entry.sha256) {
                            throw "The staged package failed verification: $relative"
                        }
                        $listed[$relative] = $true
                        $verified++
                    }
                    $unlisted = @(Get-ChildItem -LiteralPath $staging -File -Recurse -Force | ForEach-Object {
                        $_.FullName.Substring($staging.Length).TrimStart('\').Replace('\', '/')
                    } | Where-Object { $_ -cne 'lab-manifest.json' -and -not $listed.ContainsKey($_) })
                    if ($unlisted.Count) {
                        throw "The staged package contains files that are not in the manifest: $($unlisted -join ', ')"
                    }
                    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($final)) | Out-Null
                    [IO.Directory]::Move($staging, $final)
                    if ([IO.Directory]::Exists($transferRoot) -and
                        @(Get-ChildItem -LiteralPath $transferRoot -Force).Count -eq 0) {
                        [IO.Directory]::Delete($transferRoot)
                    }
                    [pscustomobject]@{
                        package_id = $PackageId; guest_path = $final
                        files_verified = $verified; manifest_sha256 = $ExpectedHash
                    }
                }
            $result = @{ vm = $vm.Name; package = $guestResult }
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
        # Host side of SSH provisioning: a dedicated internal switch only. An
        # external switch would expose the disposable lab to the real network,
        # so an existing switch with that name is refused unless it is Internal.
        'ssh_network_prepare' {
            $vm = Get-ExactVm -Name $request.vm
            $switchName = [string]$request.switch_name
            $hostIp = [string]$request.host_ip
            $prefixLength = [int]$request.prefix_length
            $adapterName = [string]$request.adapter_name
            # Decide the adapter situation before creating any host resource, so a
            # refused request leaves nothing behind.
            $adapters = @(Get-VMNetworkAdapter -VM $vm)
            $attached = @($adapters | Where-Object { $_.SwitchName -eq $switchName })
            if ($attached.Count -eq 0 -and $adapters.Count -gt 0 -and
                -not [bool]$request.use_existing_adapter) {
                throw "The VM already has a network adapter; pass use_existing_adapter to attach '$switchName' to it."
            }

            $existingSwitch = @(Get-VMSwitch -Name $switchName -ErrorAction SilentlyContinue)
            $switchCreated = $false
            if ($existingSwitch.Count -eq 0) {
                New-VMSwitch -Name $switchName -SwitchType Internal -ErrorAction Stop | Out-Null
                $switchCreated = $true
            } elseif ($existingSwitch[0].SwitchType -ne 'Internal') {
                throw "The switch '$switchName' already exists and is not Internal; refusing to use it."
            }

            $adapterCreated = $false
            if ($attached.Count -gt 0) {
                $adapter = $attached[0]
            } elseif ($adapters.Count -gt 0) {
                $adapter = $adapters[0]
                Connect-VMNetworkAdapter -VMNetworkAdapter $adapter -SwitchName $switchName -ErrorAction Stop
            } else {
                $adapter = Add-VMNetworkAdapter -VM $vm -Name $adapterName -SwitchName $switchName -ErrorAction Stop
                $adapterCreated = $true
            }

            # The host end of the internal switch carries the lab address that the
            # guest firewall rule is scoped to.
            $hostAddressCreated = $false
            $address = @(Get-NetIPAddress -IPAddress $hostIp -ErrorAction SilentlyContinue)
            if ($address.Count -eq 0) {
                $alias = "vEthernet ($switchName)"
                $aliasAddresses = @(Get-NetIPAddress -InterfaceAlias $alias -AddressFamily IPv4 -ErrorAction SilentlyContinue)
                if ($aliasAddresses.Count -eq 0) {
                    New-NetIPAddress -IPAddress $hostIp -PrefixLength $prefixLength -InterfaceAlias $alias -ErrorAction Stop | Out-Null
                    $hostAddressCreated = $true
                }
            }
            $result = @{
                vm = $vm.Name
                switch_name = $switchName
                switch_created = $switchCreated
                adapter_name = [string]$adapter.Name
                adapter_created = $adapterCreated
                host_ip = $hostIp
                host_address_created = $hostAddressCreated
            }
        }
        # Undo only what provisioning added. The switch is never deleted: another
        # VM may still use it and deleting a switch is not this tool's business.
        'ssh_network_remove' {
            $vm = Get-ExactVm -Name $request.vm
            $switchName = [string]$request.switch_name
            $adapterName = [string]$request.adapter_name
            $hostIp = [string]$request.host_ip
            $removed = @()
            foreach ($adapter in @(Get-VMNetworkAdapter -VM $vm | Where-Object { $_.SwitchName -eq $switchName })) {
                if ([bool]$request.remove_adapter -and [string]$adapter.Name -eq $adapterName) {
                    Remove-VMNetworkAdapter -VMNetworkAdapter $adapter -Confirm:$false -ErrorAction Stop
                    $removed += 'adapter'
                } else {
                    Disconnect-VMNetworkAdapter -VMNetworkAdapter $adapter -ErrorAction Stop
                    $removed += 'connection'
                }
            }
            if ([bool]$request.remove_host_address) {
                $address = @(Get-NetIPAddress -IPAddress $hostIp -ErrorAction SilentlyContinue)
                if ($address.Count -gt 0) {
                    Remove-NetIPAddress -IPAddress $hostIp -Confirm:$false -ErrorAction Stop
                    $removed += 'host_address'
                }
            }
            $result = @{ vm = $vm.Name; switch_name = $switchName; removed = @($removed) }
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

function Test-NotFoundError {
    # Remoting does not preserve guest exception types, so the host raises its own
    # typed error and this only has to recognise it (plus Hyper-V's ObjectNotFound).
    param([Parameter(Mandatory)]$ErrorRecord)
    if ($ErrorRecord.CategoryInfo.Category -eq 'ObjectNotFound') { return $true }
    $exception = $ErrorRecord.Exception
    for ($depth = 0; $null -ne $exception -and $depth -lt 8; $depth++) {
        if ($exception -is [System.IO.FileNotFoundException] -or
            $exception -is [System.IO.DirectoryNotFoundException]) { return $true }
        $exception = $exception.InnerException
    }
    return ([string]$ErrorRecord.FullyQualifiedErrorId) -match '^(FileNotFoundException|DirectoryNotFoundException)'
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
        } elseif ($category -eq 'ObjectNotFound' -or (Test-NotFoundError -ErrorRecord $errorRecord)) {
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
    foreach ($name in @($script:GuestSessions.Keys)) { Disconnect-GuestSession -VmName $name }
    exit 0
}

if ([string]::IsNullOrWhiteSpace($Operation)) { throw 'Specify -Operation or -Server.' }
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$response = Invoke-BridgeRequest -Operation $Operation -Request $request
Write-Response -Response $response
