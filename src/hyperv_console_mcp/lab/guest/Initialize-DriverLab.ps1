[CmdletBinding()]
param([Parameter(Mandatory)][string]$Root = 'C:\DriverLab')
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
if ([IO.Path]::GetFullPath($Root) -ne 'C:\DriverLab') { throw 'The lab root is fixed at C:\DriverLab.' }
foreach ($name in @('Packages', 'Transfers', 'Runs', 'Evidence', 'Tools')) {
    [IO.Directory]::CreateDirectory((Join-Path $Root $name)) | Out-Null
}
$tools = Join-Path $Root 'Tools'
$names = @('DriverLab.Common.psm1', 'Test-DriverLabPackage.ps1', 'Get-DriverLabDiagnostics.ps1', 'New-DriverLabEvidence.ps1', 'Invoke-DriverLabRun.ps1')
$files = foreach ($name in $names) {
    $path = Join-Path $tools $name
    if (-not [IO.File]::Exists($path)) { throw "Missing lab tool: $name" }
    $hash = Get-FileHash -LiteralPath $path -Algorithm SHA256
    [pscustomobject]@{ name = $name; sha256 = $hash.Hash.ToLowerInvariant(); size = (Get-Item -LiteralPath $path).Length }
}
[pscustomobject]@{
    root = $Root; initialized = $true; tools = @($files)
    security_changed = $false; payload_executed = $false
} | ConvertTo-Json -Depth 5 -Compress
