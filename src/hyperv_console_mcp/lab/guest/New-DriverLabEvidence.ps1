[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PackageId,
    [Parameter(Mandatory)][string]$RunId,
    [switch]$IncludeKernelDump
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module "$PSScriptRoot\DriverLab.Common.psm1" -Force
Assert-LabIdentifier -Value $PackageId -Name 'PackageId'
Assert-LabIdentifier -Value $RunId -Name 'RunId'
$destination = "C:\DriverLab\Evidence\$RunId"
if (Test-Path -LiteralPath $destination) { throw 'This evidence run already exists.' }
[IO.Directory]::CreateDirectory($destination) | Out-Null
$utf8NoBom = New-Object Text.UTF8Encoding($false)
& "$PSScriptRoot\Get-DriverLabDiagnostics.ps1" | ForEach-Object {
    [IO.File]::WriteAllText((Join-Path $destination 'diagnostics.json'), $_, $utf8NoBom)
}
$events = [ordered]@{}
foreach ($log in @('System', 'Application', 'Setup')) {
    try {
        $events[$log] = @(Get-WinEvent -LogName $log -MaxEvents 100 -ErrorAction Stop | ForEach-Object {
            [pscustomobject]@{
                record_id = $_.RecordId; time_utc = $_.TimeCreated.ToUniversalTime().ToString('o')
                id = $_.Id; level = [int]$_.Level; provider = $_.ProviderName; message = $_.Message
            }
        })
    } catch { $events[$log] = @() }
}
[IO.File]::WriteAllText((Join-Path $destination 'events.json'), ($events | ConvertTo-Json -Depth 6), $utf8NoBom)
$run = "C:\DriverLab\Runs\$RunId"
if (Test-Path -LiteralPath $run) {
    Get-ChildItem -LiteralPath $run -File -Recurse | ForEach-Object {
        $relative = $_.FullName.Substring($run.Length).TrimStart('\')
        $target = Assert-LabChild -Path (Join-Path (Join-Path $destination 'run') $relative) -Parent (Join-Path $destination 'run')
        [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $target
    }
}
$crash = Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl'
$minidumps = [Environment]::ExpandEnvironmentVariables([string]$crash.MinidumpDir)
if (Test-Path -LiteralPath $minidumps) {
    $dumpDestination = Join-Path $destination 'minidumps'
    [IO.Directory]::CreateDirectory($dumpDestination) | Out-Null
    Get-ChildItem -LiteralPath $minidumps -Filter '*.dmp' -File | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 3 | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $dumpDestination $_.Name)
    }
}
if ($IncludeKernelDump) {
    $kernelDump = [Environment]::ExpandEnvironmentVariables([string]$crash.DumpFile)
    if (Test-Path -LiteralPath $kernelDump) {
        Copy-Item -LiteralPath $kernelDump -Destination (Join-Path $destination 'MEMORY.DMP')
    }
}
$files = @(Get-ChildItem -LiteralPath $destination -File -Recurse | ForEach-Object {
    $hash = Get-LabFileHash -Path $_.FullName
    [pscustomobject][ordered]@{
        path = $_.FullName.Substring($destination.Length).TrimStart('\').Replace('\', '/')
        size = [Int64]$_.Length; sha256 = $hash
        # The host analyzes the newest dump, and the index is a directory
        # listing: without this the only order left is the file name.
        modified_utc = $_.LastWriteTimeUtc.ToString('o')
    }
})
$index = [pscustomobject]@{
    schema = 1; package_id = $PackageId; run_id = $RunId
    created_utc = [DateTime]::UtcNow.ToString('o'); files = $files
} | ConvertTo-Json -Depth 6
[IO.File]::WriteAllText((Join-Path $destination 'evidence.json'), $index, $utf8NoBom)
[pscustomobject]@{ run_id = $RunId; path = $destination; files = @($files).Count; payload_executed = $false } | ConvertTo-Json -Compress
