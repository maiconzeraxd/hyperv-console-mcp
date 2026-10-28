[CmdletBinding()]
param([Parameter(Mandatory)][string]$PackageId, [Parameter(Mandatory)][string]$ExpectedManifestSha256)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module "$PSScriptRoot\DriverLab.Common.psm1" -Force
if ($ExpectedManifestSha256 -notmatch '^[0-9a-f]{64}$') { throw 'Expected manifest SHA-256 is invalid.' }
$manifest = Read-LabManifest -PackageId $PackageId
$actualManifestHash = Get-LabManifestHash -Manifest $manifest
if ($actualManifestHash -cne $ExpectedManifestSha256) { throw 'The guest manifest does not match the host-approved hash.' }
$files = foreach ($entry in @($manifest.files)) {
    $parts = Get-LabRelativePath -Path ([string]$entry.path)
    $path = Assert-LabChild -Path (Join-Path "C:\DriverLab\Packages\$PackageId" ($parts -join '\')) -Parent "C:\DriverLab\Packages\$PackageId"
    if (-not [IO.File]::Exists($path)) { throw "Missing package file: $($entry.path)" }
    $size = [Int64](Get-Item -LiteralPath $path).Length
    $hash = Get-LabFileHash -Path $path
    if ($size -ne [Int64]$entry.size -or $hash -cne $entry.sha256) { throw "Package integrity mismatch: $($entry.path)" }
    [pscustomobject]@{ path = [string]$entry.path; size = $size; sha256 = $hash; verified = $true }
}
[pscustomobject]@{
    package_id = $PackageId; verified = $true; file_count = @($files).Count
    manifest_sha256 = $actualManifestHash; files = @($files); payload_executed = $false
} | ConvertTo-Json -Depth 6 -Compress
