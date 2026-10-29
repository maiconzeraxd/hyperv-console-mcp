# Guest side of the automation settings: autologon without a plain-text
# password, no lock screen, no Ctrl+Alt+Del prompt, no screen blanking, and no
# UAC consent prompt for administrators.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File guest_config.ps1 `
#       -Mode Apply -SettingsPath <json file> [-StatePath <file>]
#   powershell -NoProfile -ExecutionPolicy Bypass -File guest_config.ps1 -Mode Status
#   powershell -NoProfile -ExecutionPolicy Bypass -File guest_config.ps1 -Mode Revert
#
# Every change is recorded in the state file together with the value it
# replaced, so Revert puts the guest back the way it was. The settings file
# holds the autologon password while this script reads it and is deleted before
# anything else happens.
#
# Output is one line: HVMCP_JSON:<base64 of the JSON result>, or HVMCP_ERROR:<text>.
param(
    [Parameter(Mandatory)][ValidateSet('Apply', 'Status', 'Revert')][string]$Mode,
    [string]$SettingsPath = '',
    [string]$StatePath = 'C:\DriverLab\State\guest-config.json'
)

$ErrorActionPreference = 'Stop'

$winlogonKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$passwordLessKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device'
$personalizationKey = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization'
$systemPolicyKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
$updatePolicyKey = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
$desktopKey = 'HKCU:\Control Panel\Desktop'
$autologonSecretName = 'DefaultPassword'

$script:StateFile = $StatePath
$script:SettingsFile = $SettingsPath

# -- LSA ------------------------------------------------------------------
# Winlogon reads the autologon password from the LSA private data key
# "DefaultPassword", which is what Sysinternals Autologon uses. Keeping it out
# of the registry means it is not readable by a plain registry export.

$lsaSource = @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class HvmcpLsa
{
    [StructLayout(LayoutKind.Sequential)]
    private struct LSA_UNICODE_STRING
    {
        public ushort Length;
        public ushort MaximumLength;
        public IntPtr Buffer;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct LSA_OBJECT_ATTRIBUTES
    {
        public int Length;
        public IntPtr RootDirectory;
        public IntPtr ObjectName;
        public uint Attributes;
        public IntPtr SecurityDescriptor;
        public IntPtr SecurityQualityOfService;
    }

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern uint LsaOpenPolicy(IntPtr systemName, ref LSA_OBJECT_ATTRIBUTES attributes, uint access, out IntPtr policy);

    [DllImport("advapi32.dll", SetLastError = true, EntryPoint = "LsaStorePrivateData")]
    private static extern uint LsaStorePrivateDataRef(IntPtr policy, ref LSA_UNICODE_STRING keyName, ref LSA_UNICODE_STRING privateData);

    [DllImport("advapi32.dll", SetLastError = true, EntryPoint = "LsaStorePrivateData")]
    private static extern uint LsaStorePrivateDataNull(IntPtr policy, ref LSA_UNICODE_STRING keyName, IntPtr privateData);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern uint LsaRetrievePrivateData(IntPtr policy, ref LSA_UNICODE_STRING keyName, out IntPtr privateData);

    [DllImport("advapi32.dll")]
    private static extern uint LsaClose(IntPtr policy);

    [DllImport("advapi32.dll")]
    private static extern uint LsaFreeMemory(IntPtr buffer);

    [DllImport("advapi32.dll")]
    private static extern uint LsaNtStatusToWinError(uint status);

    private const uint PolicyAllAccess = 0x000F0FFF;
    private const uint ErrorFileNotFound = 2;

    private static IntPtr OpenPolicy()
    {
        LSA_OBJECT_ATTRIBUTES attributes = new LSA_OBJECT_ATTRIBUTES();
        attributes.Length = Marshal.SizeOf(typeof(LSA_OBJECT_ATTRIBUTES));
        IntPtr policy;
        uint status = LsaOpenPolicy(IntPtr.Zero, ref attributes, PolicyAllAccess, out policy);
        if (status != 0)
        {
            throw new Win32Exception((int)LsaNtStatusToWinError(status), "LsaOpenPolicy failed");
        }
        return policy;
    }

    private static LSA_UNICODE_STRING MakeString(string value, out IntPtr buffer)
    {
        buffer = Marshal.AllocHGlobal((value.Length + 1) * 2);
        char[] characters = value.ToCharArray();
        if (characters.Length > 0)
        {
            Marshal.Copy(characters, 0, buffer, characters.Length);
        }
        Marshal.WriteInt16(buffer, value.Length * 2, 0);
        LSA_UNICODE_STRING result = new LSA_UNICODE_STRING();
        result.Length = (ushort)(value.Length * 2);
        result.MaximumLength = (ushort)((value.Length + 1) * 2);
        result.Buffer = buffer;
        return result;
    }

    public static void Set(string name, string value)
    {
        IntPtr policy = OpenPolicy();
        IntPtr keyBuffer = IntPtr.Zero;
        IntPtr dataBuffer = IntPtr.Zero;
        try
        {
            LSA_UNICODE_STRING key = MakeString(name, out keyBuffer);
            LSA_UNICODE_STRING data = MakeString(value, out dataBuffer);
            uint status = LsaStorePrivateDataRef(policy, ref key, ref data);
            if (status != 0)
            {
                throw new Win32Exception((int)LsaNtStatusToWinError(status), "LsaStorePrivateData failed");
            }
        }
        finally
        {
            if (keyBuffer != IntPtr.Zero) { Marshal.FreeHGlobal(keyBuffer); }
            if (dataBuffer != IntPtr.Zero) { Marshal.FreeHGlobal(dataBuffer); }
            LsaClose(policy);
        }
    }

    public static void Delete(string name)
    {
        IntPtr policy = OpenPolicy();
        IntPtr keyBuffer = IntPtr.Zero;
        try
        {
            LSA_UNICODE_STRING key = MakeString(name, out keyBuffer);
            uint status = LsaStorePrivateDataNull(policy, ref key, IntPtr.Zero);
            if (status != 0 && LsaNtStatusToWinError(status) != ErrorFileNotFound)
            {
                throw new Win32Exception((int)LsaNtStatusToWinError(status), "LsaStorePrivateData failed");
            }
        }
        finally
        {
            if (keyBuffer != IntPtr.Zero) { Marshal.FreeHGlobal(keyBuffer); }
            LsaClose(policy);
        }
    }

    public static string Get(string name)
    {
        IntPtr policy = OpenPolicy();
        IntPtr keyBuffer = IntPtr.Zero;
        IntPtr data = IntPtr.Zero;
        try
        {
            LSA_UNICODE_STRING key = MakeString(name, out keyBuffer);
            uint status = LsaRetrievePrivateData(policy, ref key, out data);
            if (status != 0)
            {
                return null;
            }
            LSA_UNICODE_STRING value = (LSA_UNICODE_STRING)Marshal.PtrToStructure(data, typeof(LSA_UNICODE_STRING));
            if (value.Buffer == IntPtr.Zero)
            {
                return string.Empty;
            }
            return Marshal.PtrToStringUni(value.Buffer, value.Length / 2);
        }
        finally
        {
            if (data != IntPtr.Zero) { LsaFreeMemory(data); }
            if (keyBuffer != IntPtr.Zero) { Marshal.FreeHGlobal(keyBuffer); }
            LsaClose(policy);
        }
    }
}
'@

if (-not ('HvmcpLsa' -as [type])) {
    Add-Type -TypeDefinition $lsaSource -Language CSharp -ErrorAction Stop
}

# -- registry -------------------------------------------------------------


function Read-RegistryValue {
    param([string]$Path, [string]$Name)
    $item = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($null -eq $item) {
        return @{ existed = $false; kind = $null; value = $null }
    }
    $names = @($item.GetValueNames())
    if ($names -notcontains $Name) {
        return @{ existed = $false; kind = $null; value = $null }
    }
    return @{
        existed = $true
        kind = $item.GetValueKind($Name).ToString()
        value = $item.GetValue($Name)
    }
}

function Set-RegistryValue {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([string]$Path, [string]$Name, $Value, [string]$Kind)
    if (-not $PSCmdlet.ShouldProcess("$Path\$Name", "Set the value to $Value")) {
        return
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -Path $Path -Force -ErrorAction Stop | Out-Null
    }
    New-ItemProperty -LiteralPath $Path -Name $Name -Value $Value -PropertyType $Kind `
        -Force -ErrorAction Stop | Out-Null
}

function Remove-RegistryValue {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([string]$Path, [string]$Name)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $item = Get-Item -LiteralPath $Path
    if (@($item.GetValueNames()) -notcontains $Name) { return }
    if ($PSCmdlet.ShouldProcess("$Path\$Name", 'Remove the value')) {
        Remove-ItemProperty -LiteralPath $Path -Name $Name -Force -ErrorAction Stop
    }
}

# -- power ----------------------------------------------------------------


function Get-PowerSetting {
    param([string]$Subgroup, [string]$Setting)
    $text = (& powercfg.exe /query SCHEME_CURRENT $Subgroup $Setting) -join "`n"
    $match = [regex]::Match($text, 'Current AC Power Setting Index:\s*(0x[0-9a-fA-F]+)')
    if ($match.Success) {
        return [Convert]::ToInt32($match.Groups[1].Value.Substring(2), 16)
    }
    return $null
}

function Set-PowerSetting {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([string]$Subgroup, [string]$Setting, [int]$Value)
    if ($PSCmdlet.ShouldProcess("$Subgroup/$Setting", "Set the AC value to $Value")) {
        & powercfg.exe /setacvalueindex SCHEME_CURRENT $Subgroup $Setting $Value | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "powercfg rejected $Subgroup/$Setting=$Value (exit $LASTEXITCODE)."
        }
    }
}

# The three settings that keep the console session alive and unlocked.
$powerSettings = @(
    @{ entry = 'power'; subgroup = 'SUB_SLEEP'; setting = 'STANDBYIDLE'; value = 0 },
    @{ entry = 'power'; subgroup = 'SUB_VIDEO'; setting = 'VIDEOIDLE'; value = 0 },
    @{ entry = 'power'; subgroup = 'SUB_NONE'; setting = 'CONSOLELOCK'; value = 0 }
)

# -- state ----------------------------------------------------------------


function Get-ConfigState {
    if (-not (Test-Path -LiteralPath $script:StateFile -PathType Leaf)) {
        return @{ version = 1; entries = @{} }
    }
    $raw = Get-Content -LiteralPath $script:StateFile -Raw -ErrorAction Stop
    $parsed = $raw | ConvertFrom-Json -ErrorAction Stop
    $entries = @{}
    if ($null -ne $parsed.entries) {
        foreach ($property in $parsed.entries.PSObject.Properties) {
            $entries[$property.Name] = $property.Value
        }
    }
    return @{ version = 1; entries = $entries }
}

function Save-ConfigState {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([hashtable]$State)
    if (-not $PSCmdlet.ShouldProcess($script:StateFile, 'Write the guest configuration state')) {
        return
    }
    $directory = Split-Path -Parent $script:StateFile
    if ($directory -and -not (Test-Path -LiteralPath $directory)) {
        New-Item -ItemType Directory -Path $directory -Force -ErrorAction Stop | Out-Null
    }
    $State.updated_at = (Get-Date).ToUniversalTime().ToString('o')
    $json = $State | ConvertTo-Json -Depth 10
    Set-Content -LiteralPath $script:StateFile -Value $json -Encoding UTF8 -ErrorAction Stop
}

function Get-EntryRecord {
    param([hashtable]$State, [string]$Entry)
    if (-not $State.entries.ContainsKey($Entry)) {
        $State.entries[$Entry] = @{ values = @(); lsa = @(); power = @(); applied = @{} }
    }
    $record = $State.entries[$Entry]
    foreach ($field in @('values', 'lsa', 'power')) {
        if ($null -eq $record.$field) { $record[$field] = @() }
        else { $record[$field] = @($record[$field]) }
    }
    if ($null -eq $record.applied) { $record['applied'] = @{} }
    return $record
}

# Remember the value only the first time this entry touches it, so a second
# Apply does not record our own value as the original.
function Add-PreviousValue {
    param([hashtable]$Record, [string]$Path, [string]$Name)
    foreach ($existing in $Record.values) {
        if ($existing.path -eq $Path -and $existing.name -eq $Name) { return }
    }
    $current = Read-RegistryValue -Path $Path -Name $Name
    $Record.values += @{
        path = $Path
        name = $Name
        existed = [bool]$current.existed
        kind = $current.kind
        previous = $current.value
    }
}

function Add-PreviousPower {
    param([hashtable]$Record, [string]$Subgroup, [string]$Setting)
    foreach ($existing in $Record.power) {
        if ($existing.subgroup -eq $Subgroup -and $existing.setting -eq $Setting) { return }
    }
    $Record.power += @{
        subgroup = $Subgroup
        setting = $Setting
        previous = Get-PowerSetting -Subgroup $Subgroup -Setting $Setting
    }
}

function Add-PreviousSecret {
    param([hashtable]$Record, [string]$Name)
    foreach ($existing in $Record.lsa) {
        if ($existing.name -eq $Name) { return }
    }
    $present = $false
    try { $present = $null -ne [HvmcpLsa]::Get($Name) } catch { $present = $false }
    $Record.lsa += @{ name = $Name; existed = $present }
}

# -- live view ------------------------------------------------------------


function Get-LiveConfig {
    $autologon = Read-RegistryValue -Path $winlogonKey -Name 'AutoAdminLogon'
    $user = Read-RegistryValue -Path $winlogonKey -Name 'DefaultUserName'
    $domain = Read-RegistryValue -Path $winlogonKey -Name 'DefaultDomainName'
    $plain = Read-RegistryValue -Path $winlogonKey -Name 'DefaultPassword'
    $passwordless = Read-RegistryValue -Path $passwordLessKey -Name 'DevicePasswordLessBuildVersion'
    $lockScreen = Read-RegistryValue -Path $personalizationKey -Name 'NoLockScreen'
    $cad = Read-RegistryValue -Path $systemPolicyKey -Name 'DisableCAD'
    $uac = Read-RegistryValue -Path $systemPolicyKey -Name 'ConsentPromptBehaviorAdmin'
    $lua = Read-RegistryValue -Path $systemPolicyKey -Name 'EnableLUA'
    $update = Read-RegistryValue -Path $updatePolicyKey -Name 'NoAutoUpdate'
    $reboot = Read-RegistryValue -Path $updatePolicyKey -Name 'NoAutoRebootWithLoggedOnUsers'
    $screenSave = Read-RegistryValue -Path $desktopKey -Name 'ScreenSaveActive'
    $secretPresent = $false
    try { $secretPresent = $null -ne [HvmcpLsa]::Get($autologonSecretName) } catch { $secretPresent = $false }
    $sessions = @()
    try {
        $sessions = @(& query.exe user 2>$null | Select-Object -Skip 1 |
            Where-Object { $_ -match '\S' } | ForEach-Object { $_.Trim() })
    } catch {
        $sessions = @()
    }
    return @{
        autologon = @{
            auto_admin_logon = $autologon.value
            default_user_name = $user.value
            default_domain_name = $domain.value
            password_in_registry = [bool]$plain.existed
            password_in_lsa = $secretPresent
            device_password_less_build_version = $passwordless.value
        }
        lock_screen_disabled = ($lockScreen.value -eq 1)
        ctrl_alt_del_required = -not ($cad.value -eq 1)
        uac_consent_prompt_for_admins = $uac.value
        enable_lua = $lua.value
        windows_update_disabled = ($update.value -eq 1)
        windows_update_auto_reboot_blocked = ($reboot.value -eq 1)
        screensaver_active = $screenSave.value
        power = @{
            standby_timeout_ac = Get-PowerSetting -Subgroup 'SUB_SLEEP' -Setting 'STANDBYIDLE'
            monitor_timeout_ac = Get-PowerSetting -Subgroup 'SUB_VIDEO' -Setting 'VIDEOIDLE'
            console_lock = Get-PowerSetting -Subgroup 'SUB_NONE' -Setting 'CONSOLELOCK'
        }
        interactive_sessions = $sessions
    }
}

# -- apply ----------------------------------------------------------------


function Set-Autologon {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([hashtable]$Record, $Settings)
    if (-not $PSCmdlet.ShouldProcess('Winlogon autologon', 'Configure the autologon account')) { return }
    $userName = [string]$Settings.username
    $domain = [string]$Settings.domain
    if (-not $userName) { throw 'Autologon needs a user name.' }
    if (-not $domain) { $domain = '.' }

    Add-PreviousValue -Record $Record -Path $winlogonKey -Name 'AutoAdminLogon'
    Add-PreviousValue -Record $Record -Path $winlogonKey -Name 'DefaultUserName'
    Add-PreviousValue -Record $Record -Path $winlogonKey -Name 'DefaultDomainName'
    Add-PreviousValue -Record $Record -Path $winlogonKey -Name 'DefaultPassword'
    Add-PreviousValue -Record $Record -Path $winlogonKey -Name 'AutoLogonCount'
    Add-PreviousValue -Record $Record -Path $passwordLessKey -Name 'DevicePasswordLessBuildVersion'
    Add-PreviousSecret -Record $Record -Name $autologonSecretName

    Set-RegistryValue -Path $winlogonKey -Name 'AutoAdminLogon' -Value '1' -Kind 'String'
    Set-RegistryValue -Path $winlogonKey -Name 'DefaultUserName' -Value $userName -Kind 'String'
    Set-RegistryValue -Path $winlogonKey -Name 'DefaultDomainName' -Value $domain -Kind 'String'
    # A leftover plain-text password would be readable by anything that can read
    # the registry, and the LSA copy is what Winlogon actually uses.
    Remove-RegistryValue -Path $winlogonKey -Name 'DefaultPassword'
    # A non-zero AutoLogonCount logs on once and then stops.
    Remove-RegistryValue -Path $winlogonKey -Name 'AutoLogonCount'
    # Windows 10/11 ignore AutoAdminLogon while the passwordless device build
    # version is set.
    Set-RegistryValue -Path $passwordLessKey -Name 'DevicePasswordLessBuildVersion' -Value 0 -Kind 'DWord'
    [HvmcpLsa]::Set($autologonSecretName, [string]$Settings.secret)

    $Record.applied = @{
        enabled = $true
        user_name = $userName
        domain = $domain
        secret = 'lsa'
    }
}

function Clear-Autologon {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([hashtable]$State)
    if (-not $PSCmdlet.ShouldProcess('Winlogon autologon', 'Turn autologon off')) { return }
    if ($State.entries.ContainsKey('autologon')) {
        # The state file remembers the original values, which are better than a
        # guess at what "off" should look like.
        $restored = @(Restore-Entry -Record $State.entries['autologon'])
        $State.entries.Remove('autologon')
        return @{ restored = $restored }
    }
    Set-RegistryValue -Path $winlogonKey -Name 'AutoAdminLogon' -Value '0' -Kind 'String'
    Remove-RegistryValue -Path $winlogonKey -Name 'DefaultPassword'
    Remove-RegistryValue -Path $winlogonKey -Name 'AutoLogonCount'
    [HvmcpLsa]::Delete($autologonSecretName)
    return @{ cleared = $true }
}

function Set-LockScreen {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([hashtable]$Record)
    if (-not $PSCmdlet.ShouldProcess('NoLockScreen policy', 'Disable the lock screen')) { return }
    Add-PreviousValue -Record $Record -Path $personalizationKey -Name 'NoLockScreen'
    Set-RegistryValue -Path $personalizationKey -Name 'NoLockScreen' -Value 1 -Kind 'DWord'
    $Record.applied = @{ no_lock_screen = $true }
}

function Set-DisableCad {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([hashtable]$Record)
    if (-not $PSCmdlet.ShouldProcess('DisableCAD policy', 'Remove the Ctrl+Alt+Del logon requirement')) { return }
    Add-PreviousValue -Record $Record -Path $systemPolicyKey -Name 'DisableCAD'
    Set-RegistryValue -Path $systemPolicyKey -Name 'DisableCAD' -Value 1 -Kind 'DWord'
    $Record.applied = @{ disable_cad = $true }
}

function Set-PowerProfile {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([hashtable]$Record)
    if (-not $PSCmdlet.ShouldProcess('AC power profile', 'Stop standby, screen blanking and console lock')) { return }
    Add-PreviousValue -Record $Record -Path $desktopKey -Name 'ScreenSaveActive'
    Add-PreviousValue -Record $Record -Path $desktopKey -Name 'ScreenSaverIsSecure'
    foreach ($item in $powerSettings) {
        Add-PreviousPower -Record $Record -Subgroup $item.subgroup -Setting $item.setting
    }
    Set-RegistryValue -Path $desktopKey -Name 'ScreenSaveActive' -Value '0' -Kind 'String'
    Set-RegistryValue -Path $desktopKey -Name 'ScreenSaverIsSecure' -Value '0' -Kind 'String'
    foreach ($item in $powerSettings) {
        Set-PowerSetting -Subgroup $item.subgroup -Setting $item.setting -Value $item.value
    }
    & powercfg.exe /setactive SCHEME_CURRENT | Out-Null
    $Record.applied = @{
        standby_timeout_ac = 0
        monitor_timeout_ac = 0
        console_lock = 0
        screensaver = 'off'
    }
}

function Set-UacPolicy {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([hashtable]$Record)
    if (-not $PSCmdlet.ShouldProcess('ConsentPromptBehaviorAdmin policy', 'Stop UAC consent prompts for administrators')) { return }
    $lua = Read-RegistryValue -Path $systemPolicyKey -Name 'EnableLUA'
    if ($lua.existed -and $lua.value -eq 0) {
        throw ('EnableLUA is 0. This tool never turns UAC off; set EnableLUA=1 first. ' +
            'Turning LUA off changes the token model and breaks store apps.')
    }
    Add-PreviousValue -Record $Record -Path $systemPolicyKey -Name 'ConsentPromptBehaviorAdmin'
    Set-RegistryValue -Path $systemPolicyKey -Name 'ConsentPromptBehaviorAdmin' -Value 0 -Kind 'DWord'
    $Record.applied = @{ consent_prompt_behavior_admin = 0; enable_lua = 1 }
}

function Set-WindowsUpdatePolicy {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([hashtable]$Record)
    if (-not $PSCmdlet.ShouldProcess('Windows Update policy', 'Stop automatic updates and reboots')) { return }
    Add-PreviousValue -Record $Record -Path $updatePolicyKey -Name 'NoAutoUpdate'
    Add-PreviousValue -Record $Record -Path $updatePolicyKey -Name 'NoAutoRebootWithLoggedOnUsers'
    Set-RegistryValue -Path $updatePolicyKey -Name 'NoAutoUpdate' -Value 1 -Kind 'DWord'
    Set-RegistryValue -Path $updatePolicyKey -Name 'NoAutoRebootWithLoggedOnUsers' -Value 1 -Kind 'DWord'
    $Record.applied = @{ no_auto_update = 1; no_auto_reboot_with_logged_on_users = 1 }
}

# One independent toggle: true applies it, false puts back what it replaced,
# and an omitted (null) value leaves the setting alone.
function Invoke-ToggleEntry {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([hashtable]$State, [string]$Name, $Setting, [scriptblock]$Apply)
    if ($null -eq $Setting) { return $null }
    $action = if ($Setting) { 'Apply this setting' } else { 'Restore the previous value' }
    if (-not $PSCmdlet.ShouldProcess($Name, $action)) { return $null }
    if ($Setting) {
        $record = Get-EntryRecord -State $State -Entry $Name
        & $Apply $record
        return "${Name}:on"
    }
    if ($State.entries.ContainsKey($Name)) {
        [void](Restore-Entry -Record $State.entries[$Name])
        $State.entries.Remove($Name)
    }
    return "${Name}:off"
}

function Invoke-Apply {
    if (-not $script:SettingsFile) { throw 'Apply needs -SettingsPath.' }
    if (-not (Test-Path -LiteralPath $script:SettingsFile -PathType Leaf)) {
        throw "The settings file is missing: $script:SettingsFile"
    }
    $settings = $null
    try {
        $settings = Get-Content -LiteralPath $script:SettingsFile -Raw | ConvertFrom-Json
    } finally {
        # The file holds the autologon password; it must not outlive this read.
        Remove-Item -LiteralPath $script:SettingsFile -Force -ErrorAction SilentlyContinue
    }

    $state = Get-ConfigState
    $changed = @()

    if ($null -ne $settings.autologon) {
        if ($settings.autologon.enabled) {
            $record = Get-EntryRecord -State $state -Entry 'autologon'
            Set-Autologon -Record $record -Settings $settings.autologon
            $changed += 'autologon'
        } else {
            [void](Clear-Autologon -State $state)
            $changed += 'autologon_disabled'
        }
    }
    $outcome = Invoke-ToggleEntry -State $state -Name 'lock_screen' -Setting $settings.lock_screen `
        -Apply { param($record) Set-LockScreen -Record $record }
    if ($outcome) { $changed += $outcome }
    $outcome = Invoke-ToggleEntry -State $state -Name 'disable_cad' -Setting $settings.disable_cad `
        -Apply { param($record) Set-DisableCad -Record $record }
    if ($outcome) { $changed += $outcome }
    $outcome = Invoke-ToggleEntry -State $state -Name 'power' -Setting $settings.power `
        -Apply { param($record) Set-PowerProfile -Record $record }
    if ($outcome) { $changed += $outcome }
    $outcome = Invoke-ToggleEntry -State $state -Name 'uac' -Setting $settings.uac `
        -Apply { param($record) Set-UacPolicy -Record $record }
    if ($outcome) { $changed += $outcome }
    $outcome = Invoke-ToggleEntry -State $state -Name 'windows_update' -Setting $settings.windows_update `
        -Apply { param($record) Set-WindowsUpdatePolicy -Record $record }
    if ($outcome) { $changed += $outcome }

    if ($changed.Count -gt 0) {
        Save-ConfigState -State $state
    }
    return @{ mode = 'Apply'; changed = $changed; state_file = $script:StateFile; live = Get-LiveConfig }
}

# -- revert ---------------------------------------------------------------


function Restore-Entry {
    param([hashtable]$Record)
    $restored = @()
    foreach ($value in $Record.values) {
        if ($value.existed) {
            $kind = if ($value.kind) { $value.kind } else { 'String' }
            Set-RegistryValue -Path $value.path -Name $value.name -Value $value.previous -Kind $kind
        } else {
            Remove-RegistryValue -Path $value.path -Name $value.name
        }
        $restored += $value.name
    }
    foreach ($secret in $Record.lsa) {
        if (-not $secret.existed) {
            [HvmcpLsa]::Delete($secret.name)
            $restored += $secret.name
        }
    }
    foreach ($item in $Record.power) {
        if ($null -ne $item.previous) {
            Set-PowerSetting -Subgroup $item.subgroup -Setting $item.setting -Value ([int]$item.previous)
            $restored += $item.setting
        }
    }
    return $restored
}

function Invoke-Revert {
    $state = Get-ConfigState
    $restored = @{}
    foreach ($entry in @($state.entries.Keys)) {
        $restored[$entry] = @(Restore-Entry -Record $state.entries[$entry])
    }
    & powercfg.exe /setactive SCHEME_CURRENT | Out-Null
    if (Test-Path -LiteralPath $script:StateFile -PathType Leaf) {
        Remove-Item -LiteralPath $script:StateFile -Force -ErrorAction Stop
    }
    return @{
        mode = 'Revert'
        restored = $restored
        state_file_removed = -not (Test-Path -LiteralPath $script:StateFile -PathType Leaf)
        live = Get-LiveConfig
    }
}

# -- main -----------------------------------------------------------------


try {
    $result = switch ($Mode) {
        'Apply' { Invoke-Apply }
        'Revert' { Invoke-Revert }
        'Status' {
            $state = Get-ConfigState
            @{
                mode = 'Status'
                state_file = $script:StateFile
                state_file_present = (Test-Path -LiteralPath $script:StateFile -PathType Leaf)
                managed_entries = @($state.entries.Keys)
                live = Get-LiveConfig
            }
        }
    }
    $json = $result | ConvertTo-Json -Compress -Depth 10
    Write-Output ('HVMCP_JSON:' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
} catch {
    Write-Output ('HVMCP_ERROR:' + $_.Exception.Message.Replace("`r", ' ').Replace("`n", ' '))
    exit 1
}
