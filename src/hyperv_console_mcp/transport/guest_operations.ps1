# Structured guest operations for the SSH transport.
#
# The PowerShell Direct path runs these scriptblocks through Invoke-Command; this
# script is the same work, installed in the guest by guest_ssh_provision so both
# transports answer with the same JSON. It is invoked as:
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File guest_operations.ps1 `
#       -Operation guest_processes -Request <base64 of the JSON request>
#
# The result is one line: HVMCP_JSON:<base64 of the JSON>, or HVMCP_ERROR:<text>.
param(
    [Parameter(Mandatory)][string]$Operation,
    [Parameter(Mandatory)][string]$Request
)

$ErrorActionPreference = 'Stop'

function Get-Sha256Hex {
    param([byte[]]$Bytes)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($sha.ComputeHash($Bytes)).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-FileSha256Hex {
    param([string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
        } finally {
            $sha.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

function Get-AbsolutePath {
    param([string]$Path)
    if (-not [IO.Path]::IsPathRooted($Path)) { throw 'The guest path must be absolute.' }
    return [IO.Path]::GetFullPath($Path)
}

function Get-LabDirectoryListing {
    # Facts about one lab directory: the manifest bytes and every file with its
    # size and hash, plus whether a conflicting path already exists. Nothing is
    # compared here; the verdict is made on the host, in lab/publish.py.
    param([string]$Path, [string]$ConflictPath)
    $Path = [IO.Path]::GetFullPath($Path)
    $manifestPath = Join-Path $Path 'lab-manifest.json'
    $manifestBase64 = $null
    if ([IO.File]::Exists($manifestPath)) {
        $bytes = [IO.File]::ReadAllBytes($manifestPath)
        if ($bytes.Length -gt 4MB) { throw 'Manifest exceeds its size limit.' }
        $manifestBase64 = [Convert]::ToBase64String($bytes)
    }
    $files = @()
    if ([IO.Directory]::Exists($Path)) {
        $files = @(Get-ChildItem -LiteralPath $Path -File -Recurse -Force | ForEach-Object {
            @{
                path = $_.FullName.Substring($Path.Length).TrimStart('\').Replace('\', '/')
                size = [Int64]$_.Length
                sha256 = Get-FileSha256Hex $_.FullName
            }
        })
    }
    return @{
        path = $Path
        present = [IO.Directory]::Exists($Path)
        conflict_exists = [bool]($ConflictPath -and ([IO.Directory]::Exists($ConflictPath) -or [IO.File]::Exists($ConflictPath)))
        manifest_present = [IO.File]::Exists($manifestPath)
        manifest_base64 = $manifestBase64
        files = $files
    }
}

function Invoke-GuestOperation {
    param([string]$Name, $Body)

    switch ($Name) {
        'guest_file_info' {
            $path = [string]$Body.path
            if (-not [IO.Path]::IsPathRooted($path)) { throw 'The guest path must be absolute.' }
            $info = [IO.FileInfo]::new($path)
            if (-not $info.Exists) { return @{ file = @{ path = $path; status = 'not_found' } } }
            return @{ file = @{
                path = $info.FullName
                size = [Int64]$info.Length
                last_write_utc = $info.LastWriteTimeUtc.ToString('o')
                status = 'ok'
            } }
        }
        'guest_read_file' {
            $path = Get-AbsolutePath ([string]$Body.path)
            $maximum = [Int64]$Body.max_bytes
            $info = [IO.FileInfo]::new($path)
            if (-not $info.Exists) { return @{ file = @{ path = $path; status = 'not_found' } } }
            if ($info.Length -gt $maximum) { throw "Guest file exceeds the $maximum byte limit." }
            $bytes = [IO.File]::ReadAllBytes($info.FullName)
            return @{ file = @{
                path = $info.FullName
                size = $bytes.Length
                content_base64 = [Convert]::ToBase64String($bytes)
                sha256 = Get-Sha256Hex $bytes
                status = 'ok'
            } }
        }
        'guest_read_chunk' {
            $path = Get-AbsolutePath ([string]$Body.path)
            $offset = [Int64]$Body.offset
            $count = [int]$Body.count
            $stream = [IO.File]::OpenRead($path)
            try {
                if ($offset -lt 0 -or $offset + $count -gt $stream.Length) {
                    throw 'The requested evidence chunk is outside the file.'
                }
                $null = $stream.Seek($offset, [IO.SeekOrigin]::Begin)
                $bytes = [byte[]]::new($count)
                $read = 0
                while ($read -lt $count) {
                    $current = $stream.Read($bytes, $read, $count - $read)
                    if ($current -eq 0) { throw 'The guest file was truncated during reading.' }
                    $read += $current
                }
            } finally { $stream.Dispose() }
            return @{ chunk = @{
                offset = $offset
                size = $read
                content_base64 = [Convert]::ToBase64String($bytes)
                sha256 = Get-Sha256Hex $bytes
            } }
        }
        'guest_write_file' {
            $path = Get-AbsolutePath ([string]$Body.path)
            $directory = [IO.Path]::GetDirectoryName($path)
            if (-not [IO.Directory]::Exists($directory)) {
                if (-not [bool]$Body.create_parent) {
                    throw "Guest directory does not exist: $directory"
                }
                [IO.Directory]::CreateDirectory($directory) | Out-Null
            }
            if ([IO.File]::Exists($path) -and -not [bool]$Body.overwrite) {
                throw "Guest file already exists; overwrite was not approved: $path"
            }
            $bytes = [Convert]::FromBase64String([string]$Body.content_base64)
            $temporary = Join-Path $directory ('.hvmcp-' + [Guid]::NewGuid().ToString('N') + '.tmp')
            try {
                [IO.File]::WriteAllBytes($temporary, $bytes)
                if ([IO.File]::Exists($path)) {
                    # PowerShell turns $null into '' for a [string] parameter, and
                    # File.Replace rejects an empty backup path; [NullString]::Value
                    # is the null the overload expects.
                    [IO.File]::Replace($temporary, $path, [NullString]::Value)
                } else {
                    [IO.File]::Move($temporary, $path)
                }
            } finally {
                if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
            }
            return @{ file = @{
                path = $path; bytes_written = $bytes.Length; sha256 = Get-Sha256Hex $bytes
            } }
        }
        'guest_list_dir' {
            $path = Get-AbsolutePath ([string]$Body.path)
            if (-not [IO.Directory]::Exists($path)) { throw "Guest directory not found: $path" }
            # Directories first, then files, each by name: the page draws the list
            # in the order it arrives rather than sorting it in JavaScript.
            $entries = @([IO.DirectoryInfo]::new($path).EnumerateFileSystemInfos() |
                Sort-Object -Property @{ Expression = { -not $_.PSIsContainer } }, Name |
                Select-Object -First 500 | ForEach-Object {
                    @{
                        name = $_.Name
                        path = $_.FullName
                        type = if ($_.PSIsContainer) { 'directory' } else { 'file' }
                        size = if ($_.PSIsContainer) { $null } else { [Int64]$_.Length }
                        last_write_utc = $_.LastWriteTimeUtc.ToString('o')
                    }
                })
            return @{ directory = @{ path = $path; entries = $entries } }
        }
        'guest_make_dir' {
            $path = Get-AbsolutePath ([string]$Body.path)
            $created = -not [IO.Directory]::Exists($path)
            if ($created) { [IO.Directory]::CreateDirectory($path) | Out-Null }
            # Asking twice is not an error; the answer says whether it was made now.
            return @{ directory = @{ path = $path; created = $created } }
        }
        'guest_rename' {
            $path = Get-AbsolutePath ([string]$Body.path)
            $newName = [string]$Body.new_name
            if ($newName -notmatch '^[^\\/:*?"<>|]{1,255}$' -or $newName -in @('.', '..')) {
                throw 'new_name must be a single file or directory name.'
            }
            $parent = [IO.Path]::GetDirectoryName($path)
            if (-not $parent) { throw 'A drive root cannot be renamed.' }
            $target = [IO.Path]::Combine($parent, $newName)
            if ([IO.Directory]::Exists($target) -or [IO.File]::Exists($target)) {
                throw "The destination already exists: $target"
            }
            if ([IO.Directory]::Exists($path)) {
                [IO.Directory]::Move($path, $target)
            } elseif ([IO.File]::Exists($path)) {
                [IO.File]::Move($path, $target)
            } else {
                throw "Guest path not found: $path"
            }
            return @{ entry = @{ path = $target; previous_path = $path } }
        }
        'guest_delete' {
            $path = Get-AbsolutePath ([string]$Body.path)
            if ($path -eq [IO.Path]::GetPathRoot($path)) { throw 'A drive root cannot be deleted.' }
            $recursive = [bool]$Body.recursive
            if ([IO.File]::Exists($path)) {
                [IO.File]::Delete($path)
                return @{ deleted = @{ path = $path; type = 'file' } }
            }
            if (-not [IO.Directory]::Exists($path)) { throw "Guest path not found: $path" }
            # A non-empty directory is refused unless the caller said recursive,
            # so a stray click cannot take a tree with it.
            [IO.Directory]::Delete($path, $recursive)
            return @{ deleted = @{ path = $path; type = 'directory'; recursive = $recursive } }
        }
        'guest_assemble_file' {
            $path = [IO.Path]::GetFullPath([string]$Body.path)
            $parts = [IO.Path]::GetFullPath([string]$Body.parts_directory)
            $packageRoot = [IO.Path]::GetFullPath('C:\DriverLab\Packages\')
            $transferRoot = [IO.Path]::GetFullPath('C:\DriverLab\Transfers\')
            if (-not $path.StartsWith($packageRoot, [StringComparison]::OrdinalIgnoreCase) -and
                -not $path.StartsWith($transferRoot, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Assembly destination must remain under C:\DriverLab\Packages or C:\DriverLab\Transfers.'
            }
            if (-not $parts.StartsWith($transferRoot, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Assembly chunks must remain under C:\DriverLab\Transfers.'
            }
            if ([IO.File]::Exists($path)) { throw 'The staged package file already exists; it is immutable.' }
            $directory = [IO.Path]::GetDirectoryName($path)
            [IO.Directory]::CreateDirectory($directory) | Out-Null
            $temporary = Join-Path $directory ('.hvmcp-' + [Guid]::NewGuid().ToString('N') + '.tmp')
            $hash = [Security.Cryptography.SHA256]::Create()
            $length = [Int64]0
            try {
                $target = [IO.FileStream]::new($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                try {
                    for ($index = 0; $index -lt [int]$Body.part_count; $index++) {
                        $part = Join-Path $parts ('part-{0:D6}' -f $index)
                        $source = [IO.FileStream]::new($part, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
                        try {
                            $length += $source.Length
                            if ($length -gt [Int64]$Body.expected_size) {
                                throw 'The staged chunks exceed the expected file size.'
                            }
                            $source.CopyTo($target)
                        } finally { $source.Dispose() }
                    }
                } finally { $target.Dispose() }
                $reader = [IO.File]::OpenRead($temporary)
                try { $actual = [BitConverter]::ToString($hash.ComputeHash($reader)).Replace('-', '').ToLowerInvariant() }
                finally { $reader.Dispose() }
                if ($length -ne [Int64]$Body.expected_size -or $actual -cne [string]$Body.expected_sha256) {
                    throw 'The assembled guest file failed its size or SHA-256 check.'
                }
                [IO.File]::Move($temporary, $path)
            } finally {
                $hash.Dispose()
                if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
                if ([IO.Directory]::Exists($parts)) { [IO.Directory]::Delete($parts, $true) }
            }
            return @{ file = @{ path = $path; bytes_written = $length; sha256 = $actual } }
        }
        'guest_package_inspect' {
            $packageId = [string]$Body.package_id
            $transferId = [string]$Body.transfer_id
            if ($packageId -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]{0,63}$') { throw 'package_id is invalid.' }
            if ($transferId -notmatch '^[0-9a-f]{32}$') { throw 'transfer_id is invalid.' }
            # Facts only: the manifest bytes and every file with its size and hash.
            # The host decides what they mean; see lab/publish.py.
            $facts = Get-LabDirectoryListing -Path "C:\DriverLab\Transfers\$transferId\$packageId" `
                -ConflictPath "C:\DriverLab\Packages\$packageId"
            return @{ staged = $facts }
        }
        'guest_package_list' {
            $packageId = [string]$Body.package_id
            if ($packageId -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]{0,63}$') { throw 'package_id is invalid.' }
            # The same facts for a package that is already published; the host
            # re-checks it against the digest the caller approved.
            return @{ package = (Get-LabDirectoryListing -Path "C:\DriverLab\Packages\$packageId") }
        }
        'guest_package_commit' {
            $packageId = [string]$Body.package_id
            $transferId = [string]$Body.transfer_id
            if ($packageId -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]{0,63}$') { throw 'package_id is invalid.' }
            if ($transferId -notmatch '^[0-9a-f]{32}$') { throw 'transfer_id is invalid.' }
            $transferRoot = [IO.Path]::GetFullPath("C:\DriverLab\Transfers\$transferId")
            $staging = [IO.Path]::GetFullPath((Join-Path $transferRoot $packageId))
            $final = [IO.Path]::GetFullPath("C:\DriverLab\Packages\$packageId")
            if (-not $staging.StartsWith($transferRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
                throw 'The staging directory escapes C:\DriverLab\Transfers.'
            }
            if (-not [IO.Directory]::Exists($staging)) { throw "The staged package is missing: $staging" }
            if ([IO.Directory]::Exists($final) -or [IO.File]::Exists($final)) {
                throw 'The package directory already exists; packages are immutable.'
            }
            [IO.Directory]::CreateDirectory('C:\DriverLab\Packages') | Out-Null
            [IO.Directory]::Move($staging, $final)
            if ([IO.Directory]::Exists($transferRoot) -and
                @(Get-ChildItem -LiteralPath $transferRoot -Force).Count -eq 0) {
                [IO.Directory]::Delete($transferRoot)
            }
            return @{ package = @{ package_id = $packageId; guest_path = $final } }
        }
        'guest_processes' {
            $filter = [string]$Body.name_contains
            $processes = @(Get-Process | Where-Object {
                -not $filter -or $_.ProcessName -like "*$filter*"
            } | Sort-Object -Property CPU -Descending | Select-Object -First 200 | ForEach-Object {
                @{
                    id = $_.Id
                    name = $_.ProcessName
                    cpu_seconds = if ($null -eq $_.CPU) { $null } else { [Math]::Round($_.CPU, 3) }
                    working_set_bytes = [Int64]$_.WorkingSet64
                }
            })
            return @{ processes = $processes }
        }
        'guest_services' {
            $filter = [string]$Body.name_contains
            $includeStopped = [bool]$Body.include_stopped
            $services = @(Get-CimInstance -ClassName 'Win32_Service' | Where-Object {
                (-not $filter -or $_.Name -like "*$filter*" -or $_.DisplayName -like "*$filter*") -and
                ($includeStopped -or $_.State -eq 'Running')
            } | Sort-Object Name | Select-Object -First 200 | ForEach-Object {
                @{
                    name = $_.Name; display_name = $_.DisplayName
                    state = $_.State; start_mode = $_.StartMode
                }
            })
            return @{ services = $services }
        }
        'guest_events' {
            $filter = @{ LogName = [string]$Body.log_name; StartTime = (Get-Date).AddHours(-[int]$Body.hours) }
            if ($null -ne $Body.event_id) { $filter['Id'] = [int]$Body.event_id }
            if ($null -ne $Body.level) { $filter['Level'] = [int]$Body.level }
            $events = @()
            try {
                $events = @(Get-WinEvent -FilterHashtable $filter -MaxEvents ([int]$Body.max_events) -ErrorAction Stop |
                    ForEach-Object {
                        @{
                            record_id = $_.RecordId; time = $_.TimeCreated.ToString('o')
                            id = $_.Id; level = [int]$_.Level; provider = $_.ProviderName
                            message = $_.Message
                        }
                    })
            } catch {
                if ($_.FullyQualifiedErrorId -notmatch 'NoMatchingEventsFound') { throw }
            }
            return @{ events = $events }
        }
        default { throw "Unsupported guest operation: $Name" }
    }
}

try {
    $payload = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Request)) | ConvertFrom-Json
    $result = Invoke-GuestOperation -Name $Operation -Body $payload
    $json = $result | ConvertTo-Json -Compress -Depth 8
    Write-Output ('HVMCP_JSON:' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
} catch {
    Write-Output ('HVMCP_ERROR:' + $_.Exception.Message.Replace("`r", ' ').Replace("`n", ' '))
    exit 1
}
