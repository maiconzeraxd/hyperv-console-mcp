#Requires -Version 5.1
<#
.SYNOPSIS
    Registers the logon task that keeps the Hyper-V host running elevated.

.DESCRIPTION
    Hyper-V management needs privilege. There are two ways to have it without a
    UAC prompt on every action:

      * the account is in Hyper-V Administrators (SID S-1-5-32-578), and every
        process it starts has the rights; or
      * the host process itself runs elevated, and every client - the window, the
        AI, the command line - connects to it over the pipe without privilege.

    This script does the second one, which also covers the case where the group
    is not an option (a machine without Hyper-V Administrators, or a policy that
    removes it). It writes a small wrapper beside the operator's state and
    registers a task that runs it **at logon, with the highest run level**, as
    the operator - so the host is up, elevated, before anything asks for it, and
    the client that talks to it never needs elevation.

    The task runs only when the operator is logged on, on purpose: the host reads
    that user's Credential Manager and owns that user's pipe, so it cannot be a
    service account (docs/PROJECT.md, "the service stays per user").

.PARAMETER PythonPath
    The interpreter that runs the host. Defaults to the one running this script.

.PARAMETER SourceRoot
    The checkout or installed package root whose `src` holds the package.
    Defaults to the parent of this script's folder.

.PARAMETER Unregister
    Removes the task and the wrapper instead of creating them.

.EXAMPLE
    .\install_elevated_host.ps1
    .\install_elevated_host.ps1 -SourceRoot 'C:\tools\hyperv-console-mcp'
    .\install_elevated_host.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$PythonPath = '',
    [string]$SourceRoot = '',
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
$taskName = 'HyperVConsoleMCP-Host'
$stateDir = Join-Path $env:LOCALAPPDATA 'HyperVConsoleMCP'
$wrapper = Join-Path $stateDir 'start-host.cmd'

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Output "Removed the scheduled task $taskName."
    } else {
        Write-Output "No scheduled task $taskName to remove."
    }
    if (Test-Path $wrapper) {
        Remove-Item $wrapper -Force
        Write-Output "Removed $wrapper."
    }
    return
}

if (-not $PythonPath) {
    # pythonw keeps the window off. Find it beside whichever interpreter this
    # machine has: `python` on PATH, or the `py` launcher that Windows ships.
    $exe = $null
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) { $exe = $command.Source }
    if (-not $exe) {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($launcher) { $exe = (& py -3 -c "import sys; print(sys.executable)" 2>$null) }
    }
    if ($exe) {
        $candidate = Join-Path (Split-Path -Parent $exe) 'pythonw.exe'
        if (Test-Path $candidate) { $PythonPath = $candidate }
    }
    if (-not $PythonPath) {
        throw "pythonw.exe not found. Pass -PythonPath explicitly, e.g. -PythonPath 'C:\Python311\pythonw.exe'"
    }
}
if (-not (Test-Path $PythonPath)) { throw "Interpreter not found: $PythonPath" }

if (-not $SourceRoot) {
    $SourceRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
}
$src = Join-Path $SourceRoot 'src'
if (-not (Test-Path (Join-Path $src 'hyperv_console_mcp'))) {
    throw "No hyperv_console_mcp package under $src. Pass -SourceRoot explicitly."
}

# The wrapper exists because a scheduled task cannot carry environment
# variables: it carries one command line, and the module needs PYTHONPATH.
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
$log = Join-Path $stateDir 'service\host-launch.log'
$lines = @(
    '@echo off',
    'REM Written by scripts/install_elevated_host.ps1. The logon task runs this',
    'REM with the highest run level; pythonw keeps the window off because the host',
    'REM is a pipe server and what it has to say goes to the service log.',
    "set PYTHONPATH=$src",
    "cd /d `"$SourceRoot`"",
    "if not exist `"$(Split-Path -Parent $log)`" mkdir `"$(Split-Path -Parent $log)`"",
    "`"$PythonPath`" -m hyperv_console_mcp.service.host >> `"$log`" 2>&1"
)
# CRLF: cmd.exe misreads a batch file written with bare LF.
Set-Content -Path $wrapper -Value ($lines -join "`r`n") -Encoding ASCII
Write-Output "Wrote $wrapper"

$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$wrapper`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

$task = Get-ScheduledTask -TaskName $taskName
Write-Output ("Registered {0}: run level {1}, logon type {2}, user {3}" -f `
    $task.TaskName, $task.Principal.RunLevel, $task.Principal.LogonType, $task.Principal.UserId)
Write-Output "Start it now with: Start-ScheduledTask -TaskName $taskName"
Write-Output "Remove it with:    $PSCommandPath -Unregister"
