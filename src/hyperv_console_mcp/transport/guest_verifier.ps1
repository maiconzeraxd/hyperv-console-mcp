# Guest side of Driver Verifier: read it, turn it on, turn it off, put it back.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File guest_verifier.ps1 `
#       -Mode Status [-StatePath <file>]
#   powershell ... -File guest_verifier.ps1 -Mode Enable -Flags standard [-Drivers a.sys,b.sys]
#   powershell ... -File guest_verifier.ps1 -Mode Disable
#   powershell ... -File guest_verifier.ps1 -Mode Revert
#
# The setting is two registry values the kernel reads at boot:
# VerifyDriverLevel (a bitmask) and VerifyDrivers (the driver list). They are
# read back from there as a number and as the raw string, never from
# `verifier /query`, which is translated: a pt-BR guest prints its own wording
# on every line and a parser that matched English would report nothing on it.
# `verifier.exe` is still what applies a setting, because it is the documented
# interface and it writes exactly these values; the registry is only how they
# are read back, and how the value that was there before is remembered.
#
# Revert puts back what this tool replaced, which is not the same as Disable:
# Disable turns the verifier off whatever was configured before, Revert undoes
# this tool's own change and nothing else.
#
# Output is one line: HVMCP_JSON:<base64 of the JSON result>, or HVMCP_ERROR:<text>.
param(
    # Not mandatory: the test dot-sources this file to call its functions, and a
    # mandatory parameter would fail that binding. The mode is checked in main.
    [string]$Mode = '',
    [string]$Drivers = '',
    [string]$Flags = 'standard',
    [string]$StatePath = 'C:\DriverLab\State\verifier.json'
)

$ErrorActionPreference = 'Stop'

$verifierKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management'
$verifierExe = Join-Path $env:SystemRoot 'System32\verifier.exe'
$script:StateFile = $StatePath

# -- registry facts -------------------------------------------------------

function Get-BootTimeUtc {
    # Which boot a setting belongs to decides whether it is in force yet: the
    # kernel reads these values once, at boot.
    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    return $os.LastBootUpTime.ToUniversalTime().ToString('o')
}

function Read-VerifierFact {
    $item = Get-Item -LiteralPath $verifierKey -ErrorAction SilentlyContinue
    $mask = $null
    $driversRaw = $null
    if ($null -ne $item) {
        $names = @($item.GetValueNames())
        if ($names -contains 'VerifyDriverLevel') {
            $mask = [int64]$item.GetValue('VerifyDriverLevel')
        }
        if ($names -contains 'VerifyDrivers') {
            $driversRaw = [string]$item.GetValue('VerifyDrivers')
        }
    }
    $drivers = @()
    if (-not [string]::IsNullOrWhiteSpace($driversRaw)) {
        # The value is a list of driver names separated by spaces or commas,
        # depending on which tool wrote it last. Both are split, and the raw
        # string is reported too so nothing is lost to the split.
        $drivers = @($driversRaw -split '[,\s]+' | Where-Object { $_ })
    }
    return @{
        flags_mask = $mask
        drivers = $drivers
        drivers_raw = $driversRaw
        boot_utc = Get-BootTimeUtc
    }
}

# -- state ----------------------------------------------------------------

function Get-VerifierState {
    if (-not (Test-Path -LiteralPath $script:StateFile -PathType Leaf)) {
        return @{ version = 1; entry = $null }
    }
    $parsed = Get-Content -LiteralPath $script:StateFile -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    if ($null -eq $parsed.entry) { return @{ version = 1; entry = $null } }
    # A value read back from JSON is a PSCustomObject, which cannot be extended
    # the way a hashtable can; turn it into one so Enable can add to it.
    $entry = @{}
    foreach ($property in $parsed.entry.PSObject.Properties) { $entry[$property.Name] = $property.Value }
    if ($null -ne $entry.drivers) { $entry.drivers = @($entry.drivers) }
    return @{ version = 1; entry = $entry }
}

function Save-VerifierState {
    param([hashtable]$State)
    $directory = Split-Path -Parent $script:StateFile
    if ($directory -and -not (Test-Path -LiteralPath $directory)) {
        New-Item -ItemType Directory -Path $directory -Force -ErrorAction Stop | Out-Null
    }
    $State.updated_at = (Get-Date).ToUniversalTime().ToString('o')
    $json = $State | ConvertTo-Json -Depth 10
    Set-Content -LiteralPath $script:StateFile -Value $json -Encoding UTF8 -ErrorAction Stop
}

function Clear-VerifierState {
    if (Test-Path -LiteralPath $script:StateFile -PathType Leaf) {
        Remove-Item -LiteralPath $script:StateFile -Force -ErrorAction Stop
    }
}

# -- verifier -------------------------------------------------------------

function ConvertTo-FlagsMask {
    param([string]$Value)
    $text = $Value.Trim()
    if ($text -match '^0[xX]([0-9a-fA-F]{1,8})$') {
        return [int64][Convert]::ToUInt32($Matches[1], 16)
    }
    $number = [int64]0
    # Written out, not 0xFFFFFFFF: PowerShell 5.1 reads that literal as the
    # Int32 -1, which would reject every mask.
    if (-not [int64]::TryParse($text, [ref]$number) -or $number -lt 0 -or $number -gt 4294967295) {
        throw "-Flags must be standard, all, or a mask between 0 and 0xFFFFFFFF; got '$Value'."
    }
    return $number
}

function Invoke-Verifier {
    param([string[]]$Arguments)
    & $verifierExe @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        # verifier reports the same numeric Win32 error an API call would.
        throw "verifier $($Arguments -join ' ') exited with code $LASTEXITCODE."
    }
}

function Get-DriverName {
    param([string]$Drivers)
    $names = @($Drivers -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    foreach ($name in $names) {
        # A driver name, never a path: verifier matches it against the loaded
        # module name, and a path in this list silently verifies nothing.
        if ($name -notmatch '^[A-Za-z0-9._-]{1,120}$' -or $name -match '^\.+$') {
            throw "Invalid driver name for the verifier: $name"
        }
    }
    return $names
}

function Invoke-Enable {
    param([string]$Drivers, [string]$Flags)
    $names = @(Get-DriverName -Drivers $Drivers)
    $state = Get-VerifierState
    $before = Read-VerifierFact
    if ($null -eq $state.entry) {
        # Remember only the first time, so a second Enable does not record the
        # values this tool wrote as if they were the guest's own.
        $state.entry = @{
            flags_mask = $before.flags_mask
            drivers = @($before.drivers)
            recorded_utc = (Get-Date).ToUniversalTime().ToString('o')
        }
    }

    $arguments = @()
    if ($Flags -eq 'standard' -or $Flags -eq 'all') {
        $arguments += "/$Flags"
    } else {
        $arguments += '/flags'
        $arguments += ('0x{0:X8}' -f (ConvertTo-FlagsMask -Value $Flags))
    }
    if ($names.Count -gt 0) {
        $arguments += '/driver'
        $arguments += $names
    }
    Invoke-Verifier -Arguments $arguments

    $state.entry.applied_utc = (Get-Date).ToUniversalTime().ToString('o')
    $state.entry.applied_boot_utc = $before.boot_utc
    $state.entry.requested = @{ flags = $Flags; drivers = $names }
    Save-VerifierState -State $state
    return @{
        mode = 'Enable'
        changed = $true
        arguments = $arguments
        # The kernel reads the setting at boot, so a change made now is pending
        # until the guest restarts. This tool never restarts it.
        pending_reboot = $true
        live = Read-VerifierFact
    }
}

function Invoke-Disable {
    Invoke-Verifier -Arguments @('/reset')
    return @{
        mode = 'Disable'
        changed = $true
        # /reset clears every verifier setting, including ones that were there
        # before this tool ran; Revert is what puts those back.
        pending_reboot = $true
        live = Read-VerifierFact
    }
}

function Invoke-Revert {
    $state = Get-VerifierState
    if ($null -eq $state.entry) {
        Invoke-Verifier -Arguments @('/reset')
        return @{
            mode = 'Revert'
            restored = $false
            how = 'reset'
            reason = 'nothing was recorded, so there was no earlier setting to put back'
            pending_reboot = $true
            live = Read-VerifierFact
        }
    }
    $previousMask = $state.entry.flags_mask
    $previousDrivers = @($state.entry.drivers)
    $how = 'reset'
    if ($null -ne $previousMask -and [int64]$previousMask -ne 0) {
        $arguments = @('/flags', ('0x{0:X8}' -f [int64]$previousMask))
        if ($previousDrivers.Count -gt 0) {
            $arguments += '/driver'
            $arguments += $previousDrivers
        }
        Invoke-Verifier -Arguments $arguments
        $how = 'restored'
    } else {
        Invoke-Verifier -Arguments @('/reset')
    }
    Clear-VerifierState
    return @{
        mode = 'Revert'
        restored = $true
        how = $how
        previous = @{ flags_mask = $previousMask; drivers = $previousDrivers }
        pending_reboot = $true
        live = Read-VerifierFact
    }
}

function Invoke-Status {
    $state = Get-VerifierState
    $live = Read-VerifierFact
    # Kept as text for the operator to read. Nothing is decided from it: the
    # translated wording of a pt-BR guest would make any parse here a guess.
    $query = ''
    try {
        $query = (& $verifierExe /query 2>&1 | Out-String).Trim()
    } catch {
        $query = ''
    }
    $pending = $false
    if ($null -ne $state.entry -and $state.entry.applied_boot_utc) {
        $pending = [string]$state.entry.applied_boot_utc -ne [string]$live.boot_utc
    }
    return @{
        mode = 'Status'
        enabled = ($null -ne $live.flags_mask -and [int64]$live.flags_mask -ne 0)
        live = $live
        query_text = $query
        pending_reboot = $pending
        recorded = $state.entry
        state_file = $script:StateFile
        state_file_present = (Test-Path -LiteralPath $script:StateFile -PathType Leaf)
    }
}

# -- main -----------------------------------------------------------------

# Dot-sourcing this file loads the functions and runs nothing, which is how the
# tests drive them against a temporary state file without touching a guest.
if ($MyInvocation.InvocationName -ne '.') {
    try {
        if ($Mode -notin @('Status', 'Enable', 'Disable', 'Revert')) {
            throw "-Mode must be Status, Enable, Disable or Revert; got '$Mode'."
        }
        $result = switch ($Mode) {
            'Status' { Invoke-Status }
            'Enable' { Invoke-Enable -Drivers $Drivers -Flags $Flags }
            'Disable' { Invoke-Disable }
            'Revert' { Invoke-Revert }
        }
        $json = $result | ConvertTo-Json -Compress -Depth 10
        Write-Output ('HVMCP_JSON:' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
    } catch {
        Write-Output ('HVMCP_ERROR:' + $_.Exception.Message.Replace("`r", ' ').Replace("`n", ' '))
        exit 1
    }
}
