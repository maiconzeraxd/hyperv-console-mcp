[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PackageId,
    [Parameter(Mandatory)][string]$RunId,
    [Parameter(Mandatory)][string]$ExpectedManifestSha256,
    [Parameter(Mandatory)][string]$ApproveExecution,
    [string]$Root = 'C:\DriverLab'
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module "$PSScriptRoot\DriverLab.Common.psm1" -Force
Assert-LabIdentifier -Value $RunId -Name 'RunId'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this deliberate test in an elevated guest console.' }
if ($ApproveExecution -cne "EXECUTE $PackageId $RunId") {
    throw "Execution was not approved. Supply exactly: -ApproveExecution 'EXECUTE $PackageId $RunId'"
}
if (-not (Get-CimInstance Win32_ComputerSystem).HypervisorPresent) { throw 'Driver lab execution is restricted to a guest with a hypervisor.' }
& "$PSScriptRoot\Test-DriverLabPackage.ps1" -PackageId $PackageId -ExpectedManifestSha256 $ExpectedManifestSha256 -Root $Root | Out-Null
$entry = Get-LabEntryPointPath -PackageId $PackageId -Root $Root
$run = Join-Path $Root "Runs\$RunId"
if (Test-Path -LiteralPath $run) { throw 'This run ID already exists.' }
[IO.Directory]::CreateDirectory($run) | Out-Null
$record = [ordered]@{
    schema = 1; package_id = $PackageId; run_id = $RunId
    started_utc = [DateTime]::UtcNow.ToString('o'); entry_point = 'Run-Lab.ps1'
    manifest_sha256 = $ExpectedManifestSha256; success = $false; exit_code = $null
}
try {
    $global:LASTEXITCODE = $null
    $output = (& $entry -RunId $RunId -PackageId $PackageId 2>&1 | Out-String -Width 4096)
    $code = if ($null -ne $LASTEXITCODE) { [int]$LASTEXITCODE } else { 0 }
    $record.output = $output; $record.exit_code = $code; $record.success = ($code -eq 0)
} catch {
    $record.output = $_.Exception.ToString(); $record.exit_code = 1
}
$record.finished_utc = [DateTime]::UtcNow.ToString('o')
$json = $record | ConvertTo-Json -Depth 5
# Windows PowerShell's UTF8 encoding writes a BOM, which the host's json reader
# rejected; every other file this lab writes is BOM-less, so this one is too.
[IO.File]::WriteAllText((Join-Path $run 'result.json'), $json, (New-Object Text.UTF8Encoding($false)))
$record | ConvertTo-Json -Depth 5 -Compress
if (-not $record.success) { exit 1 }
