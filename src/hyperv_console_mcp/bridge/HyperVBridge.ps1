[CmdletBinding()]
param(
    [string]$Operation,
    [switch]$Server
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

function Write-Response {
    param([Parameter(Mandatory)][hashtable]$Response)
    $json = ConvertTo-Json -InputObject $Response -Depth 8 -Compress
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
    [Console]::Out.WriteLine("HVMCP1:$encoded")
}

function Get-ExactVm {
    param([Parameter(Mandatory)][string]$Name)
    $matches = @(Get-VM -Name $Name | Where-Object { $_.Name -eq $Name })
    if ($matches.Count -ne 1) {
        throw "Expected exactly one VM named '$Name'; found $($matches.Count)."
    }
    return $matches[0]
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
            switch ([string]$request.action) {
                'start' { Start-VM -VM $vm -Confirm:$false | Out-Null }
                'shutdown' { Stop-VM -VM $vm -Confirm:$false | Out-Null }
                'save' { Stop-VM -VM $vm -Save -Confirm:$false | Out-Null }
                'turn_off' { Stop-VM -VM $vm -TurnOff -Force -Confirm:$false | Out-Null }
                'reset' { Restart-VM -VM $vm -Force -Confirm:$false | Out-Null }
                default { throw 'Unsupported power action.' }
            }
            $result = @{ vm = (Convert-Vm -Vm (Get-ExactVm -Name $request.vm)); action = $request.action }
        }
        'checkpoint_list' {
            $vm = Get-ExactVm -Name $request.vm
            $checkpoints = @(Get-VMSnapshot -VM $vm | ForEach-Object { Convert-Checkpoint -Snapshot $_ })
            $result = @{ vm = $vm.Name; checkpoints = $checkpoints; count = $checkpoints.Count }
        }
        'checkpoint_create' {
            $vm = Get-ExactVm -Name $request.vm
            $snapshot = Checkpoint-VM -VM $vm -SnapshotName ([string]$request.name) -Confirm:$false
            $result = @{ checkpoint = (Convert-Checkpoint -Snapshot $snapshot) }
        }
        'checkpoint_restore' {
            $vm = Get-ExactVm -Name $request.vm
            $snapshot = Get-ExactCheckpoint -Vm $vm -SnapshotId $request.snapshot_id
            Restore-VMSnapshot -VMSnapshot $snapshot -Confirm:$false
            $result = @{ restored = $true; checkpoint = (Convert-Checkpoint -Snapshot $snapshot) }
        }
        'checkpoint_delete' {
            $vm = Get-ExactVm -Name $request.vm
            $snapshot = Get-ExactCheckpoint -Vm $vm -SnapshotId $request.snapshot_id
            $converted = Convert-Checkpoint -Snapshot $snapshot
            Remove-VMSnapshot -VMSnapshot $snapshot -Confirm:$false
            $result = @{ deleted = $true; checkpoint = $converted }
        }
        'guest_exec' {
            $vm = Get-ExactVm -Name $request.vm
            $credential = New-GuestCredential -Request $request
            $encodedCommand = Convert-ToBase64Text -Text ([string]$request.command)
            $guestResult = Invoke-Command -VMName $vm.Name -Credential $credential `
                -ArgumentList $encodedCommand, ([int]$request.max_output_chars) -ScriptBlock {
                    param($Encoded, $MaximumCharacters)
                    $command = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Encoded))
                    $output = ''
                    $errorMessage = ''
                    $success = $true
                    $exitCode = 0
                    try {
                        $ErrorActionPreference = 'Stop'
                    $global:LASTEXITCODE = $null
                        $output = (& ([ScriptBlock]::Create($command)) 2>&1 | Out-String -Width 4096)
                        if ($null -ne $LASTEXITCODE) {
                            $exitCode = [int]$LASTEXITCODE
                            if ($exitCode -ne 0) { $success = $false }
                        }
                    } catch {
                        $success = $false
                        $exitCode = 1
                        $errorMessage = $_.Exception.Message
                    }
                    $truncated = $output.Length -gt $MaximumCharacters
                    if ($truncated) { $output = $output.Substring(0, $MaximumCharacters) }
                    [pscustomobject]@{
                        success = $success
                        exit_code = $exitCode
                        output = $output
                        error = $errorMessage
                        output_truncated = $truncated
                    }
                }
            $result = @{ vm = $vm.Name; execution = $guestResult }
        }
        'guest_read_file' {
            $vm = Get-ExactVm -Name $request.vm
            $credential = New-GuestCredential -Request $request
            $encodedPath = Convert-ToBase64Text -Text ([string]$request.path)
            $guestResult = Invoke-Command -VMName $vm.Name -Credential $credential `
                -ArgumentList $encodedPath, ([int]$request.max_bytes) -ScriptBlock {
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
            $credential = New-GuestCredential -Request $request
            $encodedPath = Convert-ToBase64Text -Text ([string]$request.path)
            $guestResult = Invoke-Command -VMName $vm.Name -Credential $credential `
                -ArgumentList $encodedPath, ([string]$request.content_base64),
                    ([bool]$request.overwrite), ([bool]$request.create_parent) -ScriptBlock {
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
            $pressed = @()
            try {
                foreach ($key in $request.keys) {
                    $code = [uint32]$codes[[string]$key]
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
        'debug_serial_status' {
            $vm = Get-ExactVm -Name $request.vm
            $ports = @(
                Get-VMComPort -VM $vm | ForEach-Object {
                    [ordered]@{ number = [int]$_.Number; path = $_.Path; debugger_mode = $_.DebuggerMode.ToString() }
                }
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
            $guestConfigured = $false
            if ([bool]$request.configure_guest) {
                $credential = New-GuestCredential -Request $request
                Invoke-Command -VMName $vm.Name -Credential $credential -ScriptBlock {
                    $dbg = (& bcdedit.exe /dbgsettings SERIAL DEBUGPORT:1 BAUDRATE:115200 2>&1 | Out-String)
                    if ($LASTEXITCODE -ne 0) { throw "bcdedit /dbgsettings failed: $dbg" }
                    $debug = (& bcdedit.exe /debug '{current}' on 2>&1 | Out-String)
                    if ($LASTEXITCODE -ne 0) { throw "bcdedit /debug failed: $debug" }
                }
                $guestConfigured = $true
            }
            $result = @{
                vm = $vm.Name
                port_number = 1
                pipe_path = $port.Path
                debugger_mode = $port.DebuggerMode.ToString()
                guest_debug_enabled = $guestConfigured
                baud_rate = 115200
                reboot_required = $guestConfigured
                windbg_arguments = "-k com:pipe,port=$pipePath,baud=115200,reconnect,resets=0"
                next_steps = @(
                    'Start WinDbg with the returned arguments before starting the VM to catch early boot.'
                    'Do not enable Enhanced Session mode while stopped at a kernel breakpoint.'
                    'Checkpoint before driver testing; kernel debugging does not prevent a bugcheck.'
                )
            }
        }
    }
    return $result
}

function Invoke-BridgeRequest {
    param([Parameter(Mandatory)][string]$Operation, [Parameter(Mandatory)]$Request)
    try {
        if ($Request.version -ne 1) { throw 'Unsupported bridge request version.' }
        $result = Invoke-BridgeOperation -Operation $Operation -Request $Request
        return @{ ok = $true; result = $result }
    } catch {
        $message = $_.Exception.Message
        $code = 'bridge_error'
        if ($message -match 'permission|permiss|access denied|acesso negado') {
            $code = 'access_denied'
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
