#Requires -Version 5.1
<#
.SYNOPSIS
    Installs Hyper-V Console MCP on this host, or says why it cannot.

.DESCRIPTION
    This is the advanced install path. The signed installer and `winget` end in
    the same routine, so there is nothing here that the other two do not do -
    this one just lets you read every step before it runs.

    It checks that this machine can run Hyper-V, offers to enable it when it is
    installed but switched off, writes the MCP client's own configuration file,
    adds the Start Menu shortcut and finishes with a self-test that asks the
    service and Hyper-V whether they answer.

    Every step is idempotent: running it again repairs a partial install instead
    of duplicating one. It never reboots without asking, and it never stores a
    secret: the guest password lives in the Windows Credential Manager.

.PARAMETER Check
    Report what this host needs and change nothing. Exit code 0 means the
    install can go ahead.

.PARAMETER Uninstall
    Remove the MCP client entry and the shortcut. Saved credentials and the
    state folder are kept unless you say otherwise.

.PARAMETER RemoveCredentials
    With -Uninstall: also delete this user's saved guest passwords and KDnet keys.

.PARAMETER RemoveData
    With -Uninstall: also delete the state, event and evidence folders.

.PARAMETER McpProfile
    Write HYPERV_MCP_PROFILE=safe or lab into the client's entry.

.PARAMETER Uvx
    Write `uvx hyperv-console-mcp` instead of this interpreter.

.EXAMPLE
    .\install.ps1 -Check

.EXAMPLE
    .\install.ps1

.EXAMPLE
    .\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch] $Check,
    [switch] $Uninstall,
    [switch] $RemoveCredentials,
    [switch] $RemoveData,
    [ValidateSet('safe', 'lab')]
    [string] $McpProfile,
    [switch] $Uvx,
    [switch] $NoShortcut,
    [switch] $NoSelfTest
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-Runtime {
    # A bundled build keeps its own executable beside this script; a package
    # install puts `hyperv-setup` on PATH; a developer checkout has neither and
    # runs the module from the interpreter that installed it.
    if ($PSScriptRoot) {
        foreach ($name in @('hyperv-setup.exe', 'hyperv-console-mcp.exe')) {
            $candidate = Join-Path $PSScriptRoot $name
            if (Test-Path -LiteralPath $candidate) {
                return @{ File = $candidate; Prefix = @() }
            }
        }
    }
    $setup = Get-Command 'hyperv-setup' -ErrorAction SilentlyContinue
    if ($setup) {
        return @{ File = $setup.Source; Prefix = @() }
    }
    $python = Get-Command 'python' -ErrorAction SilentlyContinue
    if ($python) {
        return @{ File = $python.Source; Prefix = @('-m', 'hyperv_console_mcp.setup') }
    }
    throw ('Hyper-V Console MCP is not installed here and no Python was found. ' +
           'Download the installer from the releases page instead.')
}

function Invoke-Runtime {
    # Out-Host keeps the routine's own output out of this function's return
    # value: the caller wants the exit code, and PowerShell would otherwise
    # hand back an array of lines ending in it.
    param([hashtable] $Runtime, [string[]] $Arguments)
    & $Runtime.File @($Runtime.Prefix + $Arguments) | Out-Host
    return $LASTEXITCODE
}

function Get-Plan {
    param([hashtable] $Runtime)
    $json = & $Runtime.File @($Runtime.Prefix + @('--check')) 2>$null
    if (-not $json) {
        throw 'The setup routine printed nothing. Run `hyperv-setup --check` by hand to see why.'
    }
    return ($json | Out-String | ConvertFrom-Json)
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Confirm-Choice {
    param([string] $Question)
    # A non-interactive run (a deployment script, a pipe) must not sit waiting
    # for an answer nobody can give; the safe answer is "keep it".
    if (-not [Environment]::UserInteractive) { return $false }
    try {
        return $Question -and ((Read-Host "$Question [y/N]") -match '^(y|yes|s|sim)$')
    } catch {
        return $false
    }
}

function Invoke-Elevated {
    # One UAC prompt, for the one step that needs it, and only from a file: a
    # piped script has no path to relaunch, so it says what to run instead.
    param([string[]] $Arguments)
    if (-not $PSCommandPath) {
        Write-Output 'Hyper-V needs an elevated PowerShell. Open one and run this script again.'
        return $false
    }
    $list = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath) + $Arguments
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $list -Wait
    return $true
}

function Register-Resume {
    # Enabling Hyper-V needs a restart, and the operator should not have to
    # remember what to run afterwards. RunOnce deletes itself once it fires.
    param([string] $ScriptPath)
    if (-not $ScriptPath) { return $false }
    $command = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + $ScriptPath + '"'
    New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce' `
        -Name 'HyperVConsoleMCPSetup' -Value $command -PropertyType String -Force | Out-Null
    return $true
}

$runtime = Get-Runtime

if ($Uninstall) {
    $arguments = @('--uninstall')
    if ($RemoveCredentials -or (Confirm-Choice 'Also delete the saved guest passwords and KDnet keys?')) {
        $arguments += '--remove-credentials'
    }
    if ($RemoveData -or (Confirm-Choice 'Also delete the state, event and evidence folders?')) {
        $arguments += '--remove-data'
    }
    exit (Invoke-Runtime -Runtime $runtime -Arguments $arguments)
}

$plan = Get-Plan -Runtime $runtime

if ($Check) {
    Write-Output $plan.message
    if ($plan.action -eq 'proceed') { exit 0 }
    exit 1
}

switch ($plan.action) {
    'stop' {
        Write-Output $plan.message
        exit 1
    }
    'enable-hyperv' {
        Write-Output $plan.message
        if (-not (Test-Administrator)) {
            $relaunched = Invoke-Elevated -Arguments @()
            if (-not $relaunched) { exit 1 }
            exit 0
        }
        $feature = Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V -All -NoRestart
        if ($feature.RestartNeeded) {
            if (Register-Resume -ScriptPath $PSCommandPath) {
                Write-Output ('Hyper-V is enabled. Restart this machine and the install continues by ' +
                            'itself at your next sign-in.')
            } else {
                Write-Output ('Hyper-V is enabled. Restart this machine, then run this script again ' +
                            'to finish.')
            }
        } else {
            Write-Output 'Hyper-V is enabled. Run this installer again to finish.'
        }
        exit 0
    }
    default {
        $arguments = @()
        if ($McpProfile) { $arguments += @('--profile', $McpProfile) }
        if ($Uvx) { $arguments += '--uvx' }
        if ($NoShortcut) { $arguments += '--no-shortcut' }
        if ($NoSelfTest) { $arguments += '--no-self-test' }
        exit (Invoke-Runtime -Runtime $runtime -Arguments $arguments)
    }
}
