[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PackageId,
    [Parameter(Mandatory)][string]$ExpectedManifestSha256,
    [string]$Root = 'C:\DriverLab'
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module "$PSScriptRoot\DriverLab.Common.psm1" -Force
if ($ExpectedManifestSha256 -notmatch '^[0-9a-f]{64}$') { throw 'Expected manifest SHA-256 is invalid.' }
$packageRoot = Join-Path $Root "Packages\$PackageId"
$manifestPath = Join-Path $packageRoot 'lab-manifest.json'
if (-not [IO.File]::Exists($manifestPath)) { throw 'The package is missing its manifest.' }
# The approved digest is of the manifest file as the host wrote it, so it is the
# bytes on disk that are hashed. Re-serializing the parsed document here is what
# used to make the guest and the host disagree about canonical JSON.
$actualManifestHash = Get-LabFileHash -Path $manifestPath
if ($actualManifestHash -cne $ExpectedManifestSha256) { throw 'The guest manifest does not match the host-approved hash.' }
$manifest = Read-LabManifest -PackageId $PackageId -Root $Root
$files = foreach ($entry in @($manifest.files)) {
    $parts = Get-LabRelativePath -Path ([string]$entry.path)
    $path = Assert-LabChild -Path (Join-Path $packageRoot ($parts -join '\')) -Parent $packageRoot
    if (-not [IO.File]::Exists($path)) { throw "Missing package file: $($entry.path)" }
    $size = [Int64](Get-Item -LiteralPath $path).Length
    $hash = Get-LabFileHash -Path $path
    if ($size -ne [Int64]$entry.size -or $hash -cne $entry.sha256) { throw "Package integrity mismatch: $($entry.path)" }
    [pscustomobject]@{ path = [string]$entry.path; size = $size; sha256 = $hash; verified = $true }
}
# A file the manifest does not describe would be staged but never verified.
$listed = @{}
foreach ($entry in @($manifest.files)) { $listed[[string]$entry.path] = $true }
$unlisted = @(Get-ChildItem -LiteralPath $packageRoot -File -Recurse | ForEach-Object {
    $_.FullName.Substring($packageRoot.Length).TrimStart('\').Replace('\', '/')
} | Where-Object { $_ -cne 'lab-manifest.json' -and -not $listed.ContainsKey($_) })
if ($unlisted.Count) {
    throw "The package directory contains files that are not in the manifest: $($unlisted -join ', ')"
}
[pscustomobject]@{
    package_id = $PackageId; verified = $true; file_count = @($files).Count
    manifest_sha256 = $actualManifestHash; files = @($files); payload_executed = $false
} | ConvertTo-Json -Depth 6 -Compress
