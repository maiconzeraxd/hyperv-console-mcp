[CmdletBinding()]
param()
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$os = Get-CimInstance Win32_OperatingSystem
$computer = Get-CimInstance Win32_ComputerSystem
$secureBoot = $null
try { $secureBoot = [bool](Confirm-SecureBootUEFI -ErrorAction Stop) }
catch { $secureBoot = $null }  # Legacy BIOS, or the cmdlet is unavailable on this guest.
$deviceGuard = Get-CimInstance -Namespace 'root\Microsoft\Windows\DeviceGuard' -ClassName 'Win32_DeviceGuard' -ErrorAction SilentlyContinue
$crash = Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl'
$boot = (& bcdedit.exe /enum '{current}' 2>&1 | Out-String)
$debug = (& bcdedit.exe /enum '{dbgsettings}' 2>&1 | Out-String)
[pscustomobject]@{
    collected_utc = [DateTime]::UtcNow.ToString('o')
    computer_name = $env:COMPUTERNAME
    hypervisor_present = [bool]$computer.HypervisorPresent
    windows_caption = $os.Caption
    windows_version = $os.Version
    last_boot_utc = $os.LastBootUpTime.ToUniversalTime().ToString('o')
    free_system_drive_bytes = [Int64](Get-PSDrive -Name $env:SystemDrive.TrimEnd(':')).Free
    secure_boot = $secureBoot
    test_signing = [bool]($boot -match 'testsigning\s+Yes')
    debug_enabled = [bool]($boot -match 'debug\s+Yes')
    debug_settings = $debug
    vbs_status = if ($null -ne $deviceGuard) { [int]$deviceGuard.VirtualizationBasedSecurityStatus } else { $null }
    hvci_status = if ($null -ne $deviceGuard) { [int]$deviceGuard.CodeIntegrityPolicyEnforcementStatus } else { $null }
    crash_dump_type = [int]$crash.CrashDumpEnabled
    crash_dump_file = [string]$crash.DumpFile
    minidump_directory = [string]$crash.MinidumpDir
    automatic_reboot = [bool]$crash.AutoReboot
    security_changed = $false
    payload_executed = $false
} | ConvertTo-Json -Depth 5 -Compress
