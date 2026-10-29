Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:LabReservedNames = @('CON', 'PRN', 'AUX', 'NUL') +
    @(1..9 | ForEach-Object { "COM$_" }) + @(1..9 | ForEach-Object { "LPT$_" })

function Assert-LabIdentifier {
    param([Parameter(Mandatory)][string]$Value, [Parameter(Mandatory)][string]$Name)
    if ($Value -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]{0,63}$') { throw "$Name is invalid." }
}
function Get-LabFullPath {
    param([Parameter(Mandatory)][string]$Path)
    return [IO.Path]::GetFullPath($Path)
}
function Assert-LabChild {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Parent)
    $root = [IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($root, [StringComparison]::OrdinalIgnoreCase) -or $full -eq $root.TrimEnd('\')) {
        throw "Path escapes its lab directory: $full"
    }
    return $full
}
function Get-LabRelativePath {
    param([Parameter(Mandatory)][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path) -or [IO.Path]::IsPathRooted($Path) -or
        $Path.Contains('\') -or $Path.Length -gt 240) {
        throw 'Manifest paths must be relative POSIX paths.'
    }
    $parts = @($Path.Split('/'))
    foreach ($part in $parts) {
        # Same charset the host enforces: keeps every manifest path portable and ASCII.
        if ($part -cnotmatch '^[A-Za-z0-9._-]+$') { throw 'Unsafe manifest relative path.' }
        if ($part -ceq '.' -or $part -ceq '..' -or $part.EndsWith('.')) { throw 'Unsafe manifest relative path.' }
        if ($script:LabReservedNames -contains $part.Split('.')[0].ToUpperInvariant()) {
            throw 'Unsafe manifest relative path.'
        }
    }
    return $parts
}
function Get-LabFileHash {
    param([Parameter(Mandatory)][string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
        finally { $sha.Dispose() }
    } finally { $stream.Dispose() }
}
function ConvertTo-LabJsonString {
    # Python's json.dumps(ensure_ascii=False) escapes only quote, backslash and control
    # characters. ConvertTo-Json on PowerShell 5.1 also escapes & ' < > as & and
    # friends, which would make the two canonical hashes differ.
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value)
    $builder = [Text.StringBuilder]::new()
    [void]$builder.Append('"')
    foreach ($character in $Value.ToCharArray()) {
        $code = [int]$character
        switch ($code) {
            0x22 { [void]$builder.Append('\"'); continue }
            0x5C { [void]$builder.Append('\\'); continue }
            0x08 { [void]$builder.Append('\b'); continue }
            0x0C { [void]$builder.Append('\f'); continue }
            0x0A { [void]$builder.Append('\n'); continue }
            0x0D { [void]$builder.Append('\r'); continue }
            0x09 { [void]$builder.Append('\t'); continue }
            default {
                if ($code -lt 0x20) { [void]$builder.Append('\u' + $code.ToString('x4')) }
                else { [void]$builder.Append($character) }
            }
        }
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}
function ConvertTo-LabCanonicalJson {
    param([Parameter(Mandatory)]$Value)
    if ($null -eq $Value) { return 'null' }
    if ($Value -is [bool]) { return $(if ($Value) { 'true' } else { 'false' }) }
    if ($Value -is [string]) { return ConvertTo-LabJsonString -Value $Value }
    if ($Value -is [ValueType]) { return [Convert]::ToString($Value, [Globalization.CultureInfo]::InvariantCulture) }
    if ($Value -is [System.Collections.IDictionary]) {
        $keys = [string[]]@($Value.Keys)
        [Array]::Sort($keys, [StringComparer]::Ordinal)
        $members = foreach ($key in $keys) {
            (ConvertTo-LabJsonString -Value $key) + ':' + (ConvertTo-LabCanonicalJson -Value $Value[$key])
        }
        return '{' + ($members -join ',') + '}'
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        $members = foreach ($item in $Value) { ConvertTo-LabCanonicalJson -Value $item }
        return '[' + ($members -join ',') + ']'
    }
    $map = [ordered]@{}
    foreach ($property in $Value.PSObject.Properties) { $map[$property.Name] = $property.Value }
    return ConvertTo-LabCanonicalJson -Value $map
}
function Read-LabManifest {
    param([Parameter(Mandatory)][string]$PackageId)
    Assert-LabIdentifier -Value $PackageId -Name 'PackageId'
    $path = "C:\DriverLab\Packages\$PackageId\lab-manifest.json"
    $bytes = [IO.File]::ReadAllBytes($path)
    if ($bytes.Length -gt 4MB) { throw 'Manifest exceeds its size limit.' }
    $manifest = [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
    if ($manifest.schema -ne 1 -or $manifest.package_id -ne $PackageId -or @($manifest.files).Count -lt 1 -or @($manifest.files).Count -gt 512) {
        throw 'The package manifest is invalid.'
    }
    $paths = @{}
    foreach ($entry in @($manifest.files)) {
        # Validation only: Get-LabRelativePath throws for absolute, escaping, or malformed paths.
        $null = Get-LabRelativePath -Path ([string]$entry.path)
        if ($paths.ContainsKey($entry.path) -or [Int64]$entry.size -lt 0 -or $entry.sha256 -notmatch '^[0-9a-f]{64}$') {
            throw 'The manifest has a duplicate or invalid file entry.'
        }
        $paths[$entry.path] = $true
    }
    return $manifest
}
function Get-LabManifestHash {
    param([Parameter(Mandatory)]$Manifest)
    $json = ConvertTo-LabCanonicalJson -Value $Manifest
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}
Export-ModuleMember -Function @('Assert-LabIdentifier', 'Get-LabFullPath', 'Assert-LabChild', 'Get-LabRelativePath', 'Get-LabFileHash', 'ConvertTo-LabCanonicalJson', 'Read-LabManifest', 'Get-LabManifestHash')
