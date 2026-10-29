# Guest side of a test kernel-driver service: install it, start it, stop it, remove it.

#

#   powershell -NoProfile -ExecutionPolicy Bypass -File guest_driver_service.ps1 `

#       -Mode Status [-ServiceName x] [-StatePath <file>]

#   powershell ... -File guest_driver_service.ps1 -Mode Install -ServiceName x `

#       -ImagePath C:\DriverLab\Packages\...\x.sys -StartType demand

#   powershell ... -File guest_driver_service.ps1 -Mode Start  -ServiceName x

#   powershell ... -File guest_driver_service.ps1 -Mode Stop   -ServiceName x

#   powershell ... -File guest_driver_service.ps1 -Mode Remove -ServiceName x

#

# The Service Control Manager is driven through advapi32 rather than `sc.exe` or

# `New-Service`: those answer with translated text, and the numbers they hide are

# exactly the ones that matter here. StartService's own error code and the

# driver's NTSTATUS (SERVICE_STATUS.dwServiceSpecificExitCode, set when

# dwWin32ExitCode is ERROR_SERVICE_SPECIFIC_ERROR) both come back as numbers.

#

# This program never bypasses Driver Signature Enforcement. A driver the guest

# refuses to load is reported with the code it was refused with; test signing is

# a guest boot setting the operator owns (guest_config.ps1), not something that

# is switched here to make a load succeed.

#

# Output is one line: HVMCP_JSON:<base64 of the JSON result>, or HVMCP_ERROR:<text>.

param(

    # Not mandatory: the test dot-sources this file to call its functions, and a

    # mandatory parameter would fail that binding. The mode is checked in main.

    [string]$Mode = '',

    [string]$ServiceName = '',

    [string]$ImagePath = '',

    [string]$StartType = 'demand',

    [string]$StatePath = 'C:\DriverLab\State\driver-services.json'

)



$ErrorActionPreference = 'Stop'



$script:StateFile = $StatePath

$script:Marshal = [System.Runtime.InteropServices.Marshal]
# A real NULL for the SCM's string parameters. Kept inline: assigning it to a
# variable turns it back into an empty string, which the SCM reads as a name.



# -- Service Control Manager ------------------------------------------------



# One Add-Type per session; a second call with the same type name throws.

$script:ScmSignature = @'

using System;

using System.Runtime.InteropServices;



namespace HvmcpScm

{

    [StructLayout(LayoutKind.Sequential)]

    public struct SERVICE_STATUS_PROCESS

    {

        public uint dwServiceType;

        public uint dwCurrentState;

        public uint dwControlsAccepted;

        public uint dwWin32ExitCode;

        public uint dwServiceSpecificExitCode;

        public uint dwCheckPoint;

        public uint dwWaitHint;

        public uint dwProcessId;

        public uint dwServiceFlags;

    }



    public static class Native

    {

        [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]

        public static extern IntPtr OpenSCManager(string machine, string database, uint access);



        [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]

        public static extern IntPtr CreateService(IntPtr scm, string name, string display,

            uint access, uint type, uint start, uint errorControl, string path,

            string group, IntPtr tag, string dependencies, string account, string password);



        [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]

        public static extern IntPtr OpenService(IntPtr scm, string name, uint access);



        [DllImport("advapi32.dll", SetLastError = true)]

        public static extern bool StartService(IntPtr service, uint argc, IntPtr argv);



        [DllImport("advapi32.dll", SetLastError = true)]

        public static extern bool ControlService(IntPtr service, uint control,

            ref SERVICE_STATUS_PROCESS status);



        [DllImport("advapi32.dll", SetLastError = true)]

        public static extern bool DeleteService(IntPtr service);



        [DllImport("advapi32.dll", SetLastError = true)]

        public static extern bool QueryServiceStatusEx(IntPtr service, int level,

            ref SERVICE_STATUS_PROCESS status, uint size, out uint needed);



        [DllImport("advapi32.dll", SetLastError = true)]

        public static extern bool CloseServiceHandle(IntPtr handle);

    }

}

'@



function Initialize-Scm {

    if (-not ('HvmcpScm.Native' -as [type])) {

        Add-Type -TypeDefinition $script:ScmSignature -ErrorAction Stop

    }

}



# The names are the documented SERVICE_STATE values, not the guest's wording.

function ConvertTo-StateName {

    param([int]$Code)

    switch ($Code) {

        1 { return 'stopped' }

        2 { return 'start_pending' }

        3 { return 'stop_pending' }

        4 { return 'running' }

        5 { return 'continue_pending' }

        6 { return 'pause_pending' }

        7 { return 'paused' }

        default { return "unknown_$Code" }

    }

}



function Open-Manager {

    param([uint32]$Access)

    Initialize-Scm

    # NullString, not $null: PowerShell turns $null into an empty string for a
    # [string] parameter, and the SCM reads "" as a machine name (error 123).
    $scm = [HvmcpScm.Native]::OpenSCManager(
        [System.Management.Automation.Language.NullString]::Value,
        [System.Management.Automation.Language.NullString]::Value, $Access)

    if ($scm -eq [IntPtr]::Zero) {

        $win32 = $script:Marshal::GetLastWin32Error()

        throw "OpenSCManager failed with Win32 error $win32. A kernel driver service needs an elevated guest session."

    }

    return $scm

}



# The live state of one service, or the Win32 error that explains its absence.

function Read-ServiceFact {

    param([IntPtr]$Scm, [string]$Name)

    $service = [HvmcpScm.Native]::OpenService($Scm, $Name, 0x0004)  # SERVICE_QUERY_STATUS

    if ($service -eq [IntPtr]::Zero) {

        return @{ exists = $false; win32_error = $script:Marshal::GetLastWin32Error() }

    }

    try {

        $status = New-Object HvmcpScm.SERVICE_STATUS_PROCESS

        $needed = [uint32]0

        $size = [uint32][System.Runtime.InteropServices.Marshal]::SizeOf($status)

        if (-not [HvmcpScm.Native]::QueryServiceStatusEx($service, 0, [ref]$status, $size, [ref]$needed)) {

            return @{ exists = $true; win32_error = $script:Marshal::GetLastWin32Error() }

        }

        return @{

            exists = $true

            win32_error = 0

            state_code = [int]$status.dwCurrentState

            state = ConvertTo-StateName -Code ([int]$status.dwCurrentState)

            # A driver that failed to start reports ERROR_SERVICE_SPECIFIC_ERROR

            # here and its NTSTATUS in the second field.

            win32_exit_code = [int64]$status.dwWin32ExitCode

            service_exit_code = [int64]$status.dwServiceSpecificExitCode

            process_id = [int64]$status.dwProcessId

        }

    } finally {

        [void][HvmcpScm.Native]::CloseServiceHandle($service)

    }

}



function Wait-ServiceState {

    param([IntPtr]$Scm, [string]$Name, [int]$Expected, [int]$Seconds = 10)

    $deadline = (Get-Date).AddSeconds($Seconds)

    while ((Get-Date) -lt $deadline) {

        $fact = Read-ServiceFact -Scm $Scm -Name $Name

        if (-not $fact.exists -or $fact.state_code -eq $Expected) { return $fact }

        Start-Sleep -Milliseconds 250

    }

    return Read-ServiceFact -Scm $Scm -Name $Name

}



# -- record of what this tool installed -------------------------------------



function Get-RecordedService {

    if (-not (Test-Path -LiteralPath $script:StateFile -PathType Leaf)) {

        return @{ version = 1; services = @{} }

    }

    $parsed = Get-Content -LiteralPath $script:StateFile -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop

    $services = @{}

    if ($null -ne $parsed.services) {

        foreach ($property in $parsed.services.PSObject.Properties) {

            $entry = @{}

            foreach ($field in $property.Value.PSObject.Properties) { $entry[$field.Name] = $field.Value }

            $services[$property.Name] = $entry

        }

    }

    return @{ version = 1; services = $services }

}



function Save-RecordedService {

    param([hashtable]$State)

    $directory = Split-Path -Parent $script:StateFile

    if ($directory -and -not (Test-Path -LiteralPath $directory)) {

        New-Item -ItemType Directory -Path $directory -Force -ErrorAction Stop | Out-Null

    }

    $State.updated_at = (Get-Date).ToUniversalTime().ToString('o')

    $State | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $script:StateFile -Encoding UTF8 -ErrorAction Stop

}



# -- Service Control Manager events -----------------------------------------



# A driver that fails inside DriverEntry often leaves its NTSTATUS only in the

# System log. The records are returned verbatim - this reads the guest's own

# words back to the operator and decides nothing from them.

function Get-RecentScmEvent {

    param([string]$Name, [int]$Count = 5)

    try {

        $records = Get-WinEvent -FilterHashtable @{

            LogName = 'System'; ProviderName = 'Service Control Manager'

        } -MaxEvents 60 -ErrorAction Stop

    } catch {

        return @()

    }

    $found = @()

    foreach ($record in $records) {

        $values = @($record.Properties | ForEach-Object { [string]$_.Value })

        if (($values -join ' ').IndexOf($Name, [StringComparison]::OrdinalIgnoreCase) -lt 0) { continue }

        $found += @{

            id = [int]$record.Id

            time_utc = $record.TimeCreated.ToUniversalTime().ToString('o')

            level = [int]$record.Level

            data = $values

        }

        if ($found.Count -ge $Count) { break }

    }

    return $found

}



# -- actions ----------------------------------------------------------------



function Invoke-Install {

    param([string]$ServiceName, [string]$ImagePath, [string]$StartType)

    $startCodes = @{ boot = 0; system = 1; auto = 2; demand = 3 }

    if (-not $startCodes.ContainsKey($StartType)) {

        throw "StartType must be boot, system, auto or demand; got '$StartType'."

    }

    $scm = Open-Manager -Access 0x0007  # CONNECT | CREATE_SERVICE | ENUMERATE

    try {

        $existing = Read-ServiceFact -Scm $scm -Name $ServiceName

        if ($existing.exists) {

            throw "A service named '$ServiceName' already exists (state: $($existing.state)). Remove it first, or choose another name."

        }

        $service = [HvmcpScm.Native]::CreateService(

            $scm, $ServiceName, $ServiceName, 0x10034,  # QUERY_STATUS|START|STOP|DELETE

            0x0001,                                     # SERVICE_KERNEL_DRIVER

            [uint32]$startCodes[$StartType], 0x0001,    # SERVICE_ERROR_NORMAL

            $ImagePath,
            [System.Management.Automation.Language.NullString]::Value, [IntPtr]::Zero,
            [System.Management.Automation.Language.NullString]::Value,
            [System.Management.Automation.Language.NullString]::Value,
            [System.Management.Automation.Language.NullString]::Value)

        if ($service -eq [IntPtr]::Zero) {

            $win32 = $script:Marshal::GetLastWin32Error()

            throw "CreateService failed with Win32 error $win32."

        }

        [void][HvmcpScm.Native]::CloseServiceHandle($service)

    } finally {

        [void][HvmcpScm.Native]::CloseServiceHandle($scm)

    }

    $state = Get-RecordedService

    $state.services[$ServiceName] = @{

        image_path = $ImagePath

        start_type = $StartType

        installed_utc = (Get-Date).ToUniversalTime().ToString('o')

    }

    Save-RecordedService -State $state

    return @{ action_ok = $true; win32_error = 0 }

}



function Invoke-Start {

    param([string]$ServiceName)

    $scm = Open-Manager -Access 0x0001  # SC_MANAGER_CONNECT

    try {

        $service = [HvmcpScm.Native]::OpenService($scm, $ServiceName, 0x0014)  # QUERY_STATUS|START

        if ($service -eq [IntPtr]::Zero) {

            $win32 = $script:Marshal::GetLastWin32Error()

            throw "OpenService failed with Win32 error $win32; is '$ServiceName' installed?"

        }

        $started = $false

        $win32 = 0

        try {

            $started = [HvmcpScm.Native]::StartService($service, 0, [IntPtr]::Zero)

            if (-not $started) { $win32 = $script:Marshal::GetLastWin32Error() }

        } finally {

            [void][HvmcpScm.Native]::CloseServiceHandle($service)

        }

        # A driver that failed inside DriverEntry can leave the status a moment

        # behind the call, so the state is read once more after a short settle.

        Start-Sleep -Milliseconds 500

        $fact = Read-ServiceFact -Scm $scm -Name $ServiceName

        return @{

            action_ok = ($started -or $fact.state -eq 'running')

            win32_error = $win32

            service = $fact

        }

    } finally {

        [void][HvmcpScm.Native]::CloseServiceHandle($scm)

    }

}



function Invoke-Stop {

    param([string]$ServiceName)

    $scm = Open-Manager -Access 0x0001

    try {

        $service = [HvmcpScm.Native]::OpenService($scm, $ServiceName, 0x0024)  # QUERY_STATUS|STOP

        if ($service -eq [IntPtr]::Zero) {

            $win32 = $script:Marshal::GetLastWin32Error()

            throw "OpenService failed with Win32 error $win32; is '$ServiceName' installed?"

        }

        $stopped = $false

        $win32 = 0

        try {

            $status = New-Object HvmcpScm.SERVICE_STATUS_PROCESS

            $stopped = [HvmcpScm.Native]::ControlService($service, 0x0001, [ref]$status)  # SERVICE_CONTROL_STOP

            if (-not $stopped) { $win32 = $script:Marshal::GetLastWin32Error() }

        } finally {

            [void][HvmcpScm.Native]::CloseServiceHandle($service)

        }

        $fact = Wait-ServiceState -Scm $scm -Name $ServiceName -Expected 1  # STOPPED

        return @{

            action_ok = ($stopped -or $fact.state -eq 'stopped')

            win32_error = $win32

            service = $fact

        }

    } finally {

        [void][HvmcpScm.Native]::CloseServiceHandle($scm)

    }

}



function Invoke-Remove {

    param([string]$ServiceName)

    $scm = Open-Manager -Access 0x0001

    try {

        $fact = Read-ServiceFact -Scm $scm -Name $ServiceName

        if (-not $fact.exists) {

            throw "'$ServiceName' is not installed (Win32 error $($fact.win32_error))."

        }

        if ($fact.state -ne 'stopped') {

            $null = Invoke-Stop -ServiceName $ServiceName

        }

        $service = [HvmcpScm.Native]::OpenService($scm, $ServiceName, 0x10024)  # QUERY_STATUS|STOP|DELETE

        if ($service -eq [IntPtr]::Zero) {

            $win32 = $script:Marshal::GetLastWin32Error()

            throw "OpenService failed with Win32 error $win32."

        }

        try {

            if (-not [HvmcpScm.Native]::DeleteService($service)) {

                $win32 = $script:Marshal::GetLastWin32Error()

                throw "DeleteService failed with Win32 error $win32."

            }

        } finally {

            [void][HvmcpScm.Native]::CloseServiceHandle($service)

        }

        # The entry disappears when the last handle to it closes.

        $after = Read-ServiceFact -Scm $scm -Name $ServiceName

        $state = Get-RecordedService

        $state.services.Remove($ServiceName)

        Save-RecordedService -State $state

        return @{

            action_ok = (-not $after.exists)

            win32_error = $after.win32_error

            service = $after

        }

    } finally {

        [void][HvmcpScm.Native]::CloseServiceHandle($scm)

    }

}



function Invoke-Status {

    param([string]$ServiceName)

    $state = Get-RecordedService

    $scm = Open-Manager -Access 0x0001

    try {

        $names = @()

        if ($ServiceName) {

            $names = @($ServiceName)

        } else {

            $names = @($state.services.Keys | Sort-Object)

        }

        $services = @()

        foreach ($name in $names) {

            $fact = Read-ServiceFact -Scm $scm -Name $name

            $services += @{

                name = $name

                recorded = $state.services[$name]

                live = $fact

                events = @(Get-RecentScmEvent -Name $name)

            }

        }

        return @{

            mode = 'Status'

            services = $services

            state_file = $script:StateFile

            state_file_present = (Test-Path -LiteralPath $script:StateFile -PathType Leaf)

        }

    } finally {

        [void][HvmcpScm.Native]::CloseServiceHandle($scm)

    }

}



# -- main -----------------------------------------------------------------



# Dot-sourcing this file loads the functions and runs nothing, which is how the

# tests drive them against a temporary state file without touching a guest.

if ($MyInvocation.InvocationName -ne '.') {

    try {

        if ($Mode -notin @('Status', 'Install', 'Start', 'Stop', 'Remove')) {

            throw "-Mode must be Status, Install, Start, Stop or Remove; got '$Mode'."

        }

        if ($Mode -ne 'Status' -and -not $ServiceName) {

            throw "-ServiceName is required for -Mode $Mode."

        }

        if ($Mode -eq 'Install' -and -not $ImagePath) {

            throw "-ImagePath is required for -Mode Install."

        }

        $result = switch ($Mode) {

            'Status' { Invoke-Status -ServiceName $ServiceName }

            'Install' { Invoke-Install -ServiceName $ServiceName -ImagePath $ImagePath -StartType $StartType }

            'Start' { Invoke-Start -ServiceName $ServiceName }

            'Stop' { Invoke-Stop -ServiceName $ServiceName }

            'Remove' { Invoke-Remove -ServiceName $ServiceName }

        }

        $result['mode'] = $Mode

        $result['service_name'] = $ServiceName

        $json = $result | ConvertTo-Json -Compress -Depth 10

        Write-Output ('HVMCP_JSON:' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))

    } catch {

        Write-Output ('HVMCP_ERROR:' + $_.Exception.Message.Replace("`r", ' ').Replace("`n", ' '))

        exit 1

    }

}

