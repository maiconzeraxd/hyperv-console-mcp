# Guest side of SSH provisioning, copied into the guest and run through
# PowerShell Direct. It only touches guest state: the sshd service, sshd_config,
# the administrator authorized-keys file, one inbound firewall rule scoped to the
# host's lab address, and the lab NIC's static address.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File guest_ssh_setup.ps1 `
#       -Mode Install -PublicKey '<key>' -HostIp 10.77.0.1 -GuestIp 10.77.0.2 `
#       -PrefixLength 24 -MacAddress 00155D000001 [-SshdSource <dir>] [-AllowPassword]
#
# Output is one line: HVMCP_JSON:<base64 of the JSON result>, or HVMCP_ERROR:<text>.
param(
    # Not mandatory: the host-side test dot-sources this file to exercise its
    # helpers, and a mandatory parameter would fail that binding. The mode is
    # validated in main instead, so the command line behaves the same.
    [string]$Mode = '',
    [string]$PublicKey = '',
    [string]$HostIp = '',
    [string]$GuestIp = '',
    [int]$PrefixLength = 24,
    [string]$MacAddress = '',
    [string]$SshdSource = '',
    [switch]$AllowPassword
)

$ErrorActionPreference = 'Stop'
$sshDirectory = Join-Path $env:ProgramData 'ssh'
$sshdConfig = Join-Path $sshDirectory 'sshd_config'
$authorizedKeys = Join-Path $sshDirectory 'administrators_authorized_keys'
$firewallRule = 'HyperVConsoleMCP-SSH'
$shellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

# The helpers below run in the script scope, so the values arrive through
# script-scoped names (PowerShell variables are case-insensitive, so the
# parameters cannot be shadowed by a lower-cased copy).
$script:PublicKeyLine = $PublicKey
$script:LabHostIp = $HostIp
$script:LabPrefixLength = $PrefixLength
$script:SshdPackage = $SshdSource
$script:PasswordAllowed = [bool]$AllowPassword

function Get-SshdService {
    return Get-Service -Name 'sshd' -ErrorAction SilentlyContinue
}

function Install-Sshd {
    $service = Get-SshdService
    if ($null -ne $service) { return 'already-present' }
    $capabilityError = 'not attempted'
    try {
        # Windows Server 2025 ships OpenSSH disabled; a guest with internet can
        # enable the capability instead of receiving a package from the host.
        Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' -ErrorAction Stop | Out-Null
        $service = Get-SshdService
        if ($null -ne $service) { return 'capability' }
    } catch {
        $capabilityError = $_.Exception.Message
    }
    if (-not $script:SshdPackage) {
        # A distinct marker, so the host can tell "I need the offline package" apart
        # from a real failure and retry with one instead of parsing the message.
        Write-Output ('HVMCP_NEEDS_PACKAGE:' + $capabilityError.Replace("`r", ' ').Replace("`n", ' '))
        exit 3
    }
    $installer = Join-Path $script:SshdPackage 'install-sshd.ps1'
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        throw "The offline OpenSSH package has no install-sshd.ps1: $script:SshdPackage"
    }
    & $installer
    if ($null -eq (Get-SshdService)) { throw 'install-sshd.ps1 did not register the sshd service.' }
    return 'offline-package'
}

function Set-SshdConfig {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param()
    if (-not (Test-Path -LiteralPath $sshdConfig -PathType Leaf)) {
        throw "sshd_config is missing at $sshdConfig"
    }
    $backup = "$sshdConfig.hvmcp-backup"
    if (-not (Test-Path -LiteralPath $backup -PathType Leaf)) {
        Copy-Item -LiteralPath $sshdConfig -Destination $backup -Force
    }
    $passwordLine = if ($script:PasswordAllowed) { 'PasswordAuthentication yes' } else { 'PasswordAuthentication no' }
    # sshd uses the first value it reads for a keyword, so the managed lines go
    # above the distribution's own settings (and above any Match block).
    $existing = @(Get-Content -LiteralPath $sshdConfig |
        Where-Object { $_ -notmatch '^\s*(PasswordAuthentication|PubkeyAuthentication)\s' } |
        ForEach-Object { [string]$_ })
    $managed = @(
        '# Managed by HyperVConsoleMCP: key authentication only.'
        'PubkeyAuthentication yes'
        $passwordLine
    )
    if ($PSCmdlet.ShouldProcess($sshdConfig, 'Write the managed sshd settings')) {
        Set-Content -LiteralPath $sshdConfig -Value ($managed + $existing) -Encoding ascii
    }
}

function Set-DefaultShell {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param()
    $key = 'HKLM:\SOFTWARE\OpenSSH'
    if (-not $PSCmdlet.ShouldProcess($key, "Set DefaultShell to $shellPath")) { return }
    if (-not (Test-Path -LiteralPath $key)) { New-Item -Path $key -Force | Out-Null }
    New-ItemProperty -Path $key -Name 'DefaultShell' -Value $shellPath -PropertyType String -Force | Out-Null
}

function Set-AuthorizedKey {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param()
    if (-not $script:PublicKeyLine) { throw 'No public key was supplied.' }
    if (-not (Test-Path -LiteralPath $sshDirectory)) { New-Item -ItemType Directory -Path $sshDirectory -Force | Out-Null }
    $lines = @()
    if (Test-Path -LiteralPath $authorizedKeys -PathType Leaf) {
        $lines = @(Get-Content -LiteralPath $authorizedKeys |
            Where-Object { $_.Trim() -and -not $_.StartsWith('# Managed by HyperVConsoleMCP') })
    }
    # One managed key per guest: replace any key this tool installed before.
    $lines = @($lines | Where-Object { $_ -notlike 'ssh-ed25519*HyperVConsoleMCP*' })
    $lines += "$($script:PublicKeyLine) HyperVConsoleMCP"
    if (-not $PSCmdlet.ShouldProcess($authorizedKeys, 'Install the host public key')) { return }
    Set-Content -LiteralPath $authorizedKeys -Value $lines -Encoding ascii
    # OpenSSH refuses a key file that is readable by anyone but Administrators
    # and SYSTEM, so the ACL is reset rather than inherited. The grants name the
    # well-known SIDs, not the accounts: a localized guest has no 'Administrators'
    # or 'SYSTEM' account name, and icacls fails on the English spelling there.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $aclOutput = & icacls.exe $authorizedKeys /inheritance:r `
        /grant:r '*S-1-5-32-544:F' /grant:r '*S-1-5-18:F' 2>&1 | ForEach-Object { "$_" }
    $aclExit = $LASTEXITCODE
    $ErrorActionPreference = $previousPreference
    if ($aclExit -ne 0) {
        throw ("Could not restrict the ACL of {0} (icacls exit {1}): {2}" -f
            $authorizedKeys, $aclExit, ($aclOutput -join ' '))
    }
}

function Set-FirewallRule {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param()
    if (-not $script:LabHostIp) { throw 'No host address was supplied for the firewall scope.' }
    if (-not $PSCmdlet.ShouldProcess($firewallRule, "Allow tcp/22 from $($script:LabHostIp)")) { return }
    Get-NetFirewallRule -Name $firewallRule -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue
    New-NetFirewallRule -Name $firewallRule -DisplayName 'HyperVConsoleMCP SSH (lab host only)' `
        -Direction Inbound -Protocol TCP -LocalPort 22 -RemoteAddress $script:LabHostIp `
        -Action Allow -Profile Any -ErrorAction Stop | Out-Null
}

function Set-LabAddress {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param()
    if (-not $GuestIp) { throw 'No guest address was supplied.' }
    if (-not $MacAddress) { throw 'No MAC address was supplied to identify the lab adapter.' }
    $normalized = ($MacAddress -replace '[^0-9A-Fa-f]', '').ToUpperInvariant()
    $adapter = Get-NetAdapter | Where-Object {
        ($_.MacAddress -replace '[^0-9A-Fa-f]', '').ToUpperInvariant() -eq $normalized
    } | Select-Object -First 1
    if ($null -eq $adapter) {
        throw "No guest network adapter has MAC address $MacAddress; is the lab switch attached?"
    }
    $alias = $adapter.Name
    if (-not $PSCmdlet.ShouldProcess($alias, "Set the lab address $GuestIp/$($script:LabPrefixLength)")) {
        return $alias
    }
    Set-NetIPInterface -InterfaceAlias $alias -Dhcp Disabled -ErrorAction SilentlyContinue
    foreach ($address in @(Get-NetIPAddress -InterfaceAlias $alias -AddressFamily IPv4 -ErrorAction SilentlyContinue)) {
        if ($address.IPAddress -ne $GuestIp) {
            Remove-NetIPAddress -IPAddress $address.IPAddress -InterfaceAlias $alias `
                -Confirm:$false -ErrorAction SilentlyContinue
        }
    }
    $current = @(Get-NetIPAddress -InterfaceAlias $alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -eq $GuestIp })
    if ($current.Count -eq 0) {
        New-NetIPAddress -InterfaceAlias $alias -IPAddress $GuestIp -PrefixLength $script:LabPrefixLength `
            -ErrorAction Stop | Out-Null
    }
    return $alias
}

function Get-HostKeyPublic {
    $path = Join-Path $sshDirectory 'ssh_host_ed25519_key.pub'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw 'The guest has no ed25519 host key yet; sshd creates it on first start.'
    }
    return ((Get-Content -LiteralPath $path -Raw).Trim() -split '\s+' | Select-Object -First 2) -join ' '
}

function Get-RuleRemoteAddress {
    param($Rule)
    if ($null -eq $Rule) { return $null }
    # A rule's own RemoteAddress property comes back empty; the scope lives on
    # its associated address filter, so read that instead. Status used to report
    # '' for a rule that really was scoped to the lab host.
    $filter = $Rule | Get-NetFirewallAddressFilter
    if ($null -eq $filter) { return $null }
    return [string](@($filter.RemoteAddress) -join ',')
}

function Get-ManagedConfigLine {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @() }
    # Get-Content decorates every line with the provider's note properties
    # (PSPath, PSDrive, PSProvider, ...). ConvertTo-Json then walks that whole
    # object graph, so two lines serialized to 700 KB and the result was cut by
    # the output limit. Only the text is wanted, so each line is cast to string.
    return @(Get-Content -LiteralPath $Path |
        Where-Object { $_ -match '^\s*(PasswordAuthentication|PubkeyAuthentication)\s' } |
        ForEach-Object { [string]$_ })
}

function Get-SetupState {
    $service = Get-SshdService
    $rule = @(Get-NetFirewallRule -Name $firewallRule -ErrorAction SilentlyContinue)
    $addresses = @()
    if ($MacAddress) {
        $normalized = ($MacAddress -replace '[^0-9A-Fa-f]', '').ToUpperInvariant()
        $adapter = Get-NetAdapter | Where-Object {
            ($_.MacAddress -replace '[^0-9A-Fa-f]', '').ToUpperInvariant() -eq $normalized
        } | Select-Object -First 1
        if ($null -ne $adapter) {
            $addresses = @(Get-NetIPAddress -InterfaceAlias $adapter.Name -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                ForEach-Object { $_.IPAddress })
        }
    }
    $config = Get-ManagedConfigLine -Path $sshdConfig
    $keyPresent = $false
    if (Test-Path -LiteralPath $authorizedKeys -PathType Leaf) {
        $keyPresent = [bool](Get-Content -LiteralPath $authorizedKeys |
            Where-Object { $_ -like 'ssh-ed25519*HyperVConsoleMCP*' })
    }
    return @{
        sshd_installed = ($null -ne $service)
        sshd_status = if ($null -eq $service) { $null } else { [string]$service.Status }
        sshd_start_type = if ($null -eq $service) { $null } else {
            [string](Get-CimInstance -ClassName 'Win32_Service' -Filter "Name='sshd'" -ErrorAction SilentlyContinue).StartMode
        }
        config_lines = $config
        authorized_key_present = $keyPresent
        firewall_rule_present = ($rule.Count -gt 0)
        firewall_remote_address = if ($rule.Count -eq 0) { $null } else { Get-RuleRemoteAddress -Rule $rule[0] }
        ip_addresses = $addresses
        default_shell = (Get-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name 'DefaultShell' -ErrorAction SilentlyContinue).DefaultShell
    }
}

# Dot-sourcing (the host-side test does that to reach the helpers) must not run
# the setup itself.
if ($MyInvocation.InvocationName -ne '.') {
try {
    $result = @{}
    if ($Mode -notin @('Install', 'Status', 'Remove')) {
        throw "-Mode must be Install, Status or Remove; got '$Mode'."
    }
    switch ($Mode) {
        'Install' {
            $install = Install-Sshd
            Set-Service -Name 'sshd' -StartupType Automatic -ErrorAction Stop
            Start-Service -Name 'sshd' -ErrorAction Stop
            Set-SshdConfig
            Set-DefaultShell
            Set-AuthorizedKey
            Set-FirewallRule
            $alias = Set-LabAddress
            # sshd generates its host key on first start; give it a moment.
            for ($attempt = 0; $attempt -lt 20; $attempt++) {
                if (Test-Path -LiteralPath (Join-Path $sshDirectory 'ssh_host_ed25519_key.pub')) { break }
                Start-Sleep -Milliseconds 250
            }
            $result = @{
                mode = 'Install'
                sshd_source = $install
                adapter_alias = $alias
                host_key_public = Get-HostKeyPublic
                state = Get-SetupState
            }
        }
        'Status' {
            $state = Get-SetupState
            $hostKey = $null
            try { $hostKey = Get-HostKeyPublic } catch { $hostKey = $null }
            $result = @{ mode = 'Status'; host_key_public = $hostKey; state = $state }
        }
        'Remove' {
            Get-NetFirewallRule -Name $firewallRule -ErrorAction SilentlyContinue |
                Remove-NetFirewallRule -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $authorizedKeys -PathType Leaf) {
                $lines = @(Get-Content -LiteralPath $authorizedKeys |
                    Where-Object { $_ -notlike 'ssh-ed25519*HyperVConsoleMCP*' -and
                                   -not $_.StartsWith('# Managed by HyperVConsoleMCP') })
                if ($lines.Count -gt 0) {
                    Set-Content -LiteralPath $authorizedKeys -Value $lines -Encoding ascii
                } else {
                    Remove-Item -LiteralPath $authorizedKeys -Force
                }
            }
            $removedAddress = $false
            if ($GuestIp -and $MacAddress) {
                $normalized = ($MacAddress -replace '[^0-9A-Fa-f]', '').ToUpperInvariant()
                $adapter = Get-NetAdapter | Where-Object {
                    ($_.MacAddress -replace '[^0-9A-Fa-f]', '').ToUpperInvariant() -eq $normalized
                } | Select-Object -First 1
                if ($null -ne $adapter) {
                    $address = @(Get-NetIPAddress -InterfaceAlias $adapter.Name -AddressFamily IPv4 `
                        -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -eq $GuestIp })
                    if ($address.Count -gt 0) {
                        Remove-NetIPAddress -IPAddress $GuestIp -InterfaceAlias $adapter.Name `
                            -Confirm:$false -ErrorAction SilentlyContinue
                        $removedAddress = $true
                    }
                }
            }
            $result = @{ mode = 'Remove'; removed_guest_address = $removedAddress; state = Get-SetupState }
        }
    }
    $json = $result | ConvertTo-Json -Compress -Depth 6
    Write-Output ('HVMCP_JSON:' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
} catch {
    Write-Output ('HVMCP_ERROR:' + $_.Exception.Message.Replace("`r", ' ').Replace("`n", ' '))
    exit 1
}
}
