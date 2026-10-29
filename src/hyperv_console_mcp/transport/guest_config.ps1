# Guest side of the automation settings: autologon without a plain-text
# password, no lock screen, no Ctrl+Alt+Del prompt, no screen blanking, no
# UAC consent prompt for administrators, and the driver-lab boot entries
# (test signing, kernel debugging, crash dump, HVCI/VBS).
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
    # Not mandatory: the state-file test dot-sources this file to call its
    # functions, and a mandatory parameter would fail that binding. The mode is
    # validated in main instead, so the command line behaves the same.
    [string]$Mode = '',
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
$crashControlKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl'
$deviceGuardKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard'
$hvciKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity'
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

# -- boot configuration (BCD) ---------------------------------------------
# Test signing and kernel debugging are boot entries, not registry values, so
# they are read and written through bcdedit and recorded separately.
#
# bcdedit translates the labels around each entry but not the entries
# themselves: the element name and the value are fixed tokens. A pt-BR guest
# prints the same "testsigning             Yes" an English one does. The reader
# used to compare that token against 'on', which bcdedit never prints, so test
# signing and kernel debugging read back as off on every guest, in every
# language, even when both were on. The token is normalised to on/off here, and
# 'on'/'off' is also what bcdedit accepts as input on a localized guest
# (verified against pt-BR: it takes on/off and rejects Sim).
#
# The normalised value is what goes back through bcdedit on revert. The live
# view reports the token as printed and the host reads it (bcd.py), so no
# setting is described twice.
$bcdBooleanTokens = @{
    'yes' = 'on'; 'no' = 'off'
    'on' = 'on'; 'off' = 'off'
    'true' = 'on'; 'false' = 'off'
}

function ConvertFrom-BcdEnum {
    param([string]$Text, [string]$Name)
    $match = [regex]::Match($Text, "(?m)^\s*$([regex]::Escape($Name))\s+(\S+)")
    if (-not $match.Success) {
        return @{ existed = $false; value = $null; token = $null }
    }
    $token = $match.Groups[1].Value
    $normalized = $bcdBooleanTokens[$token.ToLowerInvariant()]
    if (-not $normalized) { $normalized = $token }
    # `value` is what goes back through bcdedit on revert; `token` is what the
    # guest printed, which the live view reports so the host can read it.
    return @{ existed = $true; value = $normalized; token = $token }
}

function Read-BcdValue {
    param([string]$Name)
    $text = (& bcdedit.exe /enum '{current}' 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "bcdedit /enum failed: $text" }
    return ConvertFrom-BcdEnum -Text $text -Name $Name
}

function Set-BcdValue {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([string]$Name, [string]$Value)
    if (-not $PSCmdlet.ShouldProcess("bcdedit $Name", "Set it to $Value")) { return }
    # 'on'/'off' is the value bcdedit accepts here, in any display language.
    $output = (& bcdedit.exe /set '{current}' $Name $Value 2>&1) -join ' '
    if ($LASTEXITCODE -ne 0) {
        throw "bcdedit /set $Name $Value failed: $output"
    }
}

function Remove-BcdValue {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([string]$Name)
    if (-not $PSCmdlet.ShouldProcess("bcdedit $Name", 'Remove the value')) { return }
    # bcdedit fails when the entry is already absent, which is the state we want.
    & bcdedit.exe /deletevalue '{current}' $Name 2>&1 | Out-Null
}

# -- power ----------------------------------------------------------------


# powercfg prints its labels in the display language, so its output cannot be
# parsed for the current values: on a pt-BR guest the English label this script
# used to look for never appears and every value read back as null. The GUIDs
# below are the documented constants for the three settings, and the CIM power
# provider exposes the effective value of each plan under "{scheme}\AC\{setting}",
# which is the number powercfg would print, in any language.
$powerGuids = @{
    STANDBYIDLE = '29f6c1db-86da-48c5-9fdb-f2b67b1f44da'
    VIDEOIDLE = '3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e'
    CONSOLELOCK = '0e796bdb-100d-47d6-a2d5-f7d2daa51f51'
}

function Get-ActiveSchemeGuid {
    $key = 'HKLM:\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes'
    $active = (Get-ItemProperty -Path $key -Name 'ActivePowerScheme' -ErrorAction SilentlyContinue).ActivePowerScheme
    if ($active) { return [string]$active }
    # The scheme GUID itself is not localized, so this line is safe to match.
    $text = (& powercfg.exe /getactivescheme) -join "`n"
    $match = [regex]::Match($text, '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
    if ($match.Success) { return $match.Value }
    return $null
}

function Get-PowerSetting {
    param([string]$Setting)
    $guid = $powerGuids[$Setting]
    if (-not $guid) { return $null }
    $scheme = Get-ActiveSchemeGuid
    if (-not $scheme) { return $null }
    $entry = Get-CimInstance -Namespace root\cimv2\power -ClassName Win32_PowerSettingDataIndex `
        -ErrorAction SilentlyContinue |
        Where-Object { $_.InstanceID -like "*$scheme*\AC\*$guid*" } |
        Select-Object -First 1
    if ($null -eq $entry) { return $null }
    return [int]$entry.SettingIndexValue
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


# Values read back from the state file arrive as PSCustomObjects, which cannot be
# indexed or extended the way the records are used. Convert them, recursively, so
# a second Apply (or a Revert after a restart) works on what the first one wrote.
function ConvertTo-Hashtable {
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        $result = @{}
        foreach ($property in $Value.PSObject.Properties) {
            $result[$property.Name] = ConvertTo-Hashtable -Value $property.Value
        }
        return $result
    }
    if ($Value -is [string]) { return $Value }
    if ($Value -is [System.Collections.IEnumerable]) {
        $items = @(foreach ($item in $Value) { ConvertTo-Hashtable -Value $item })
        # The leading comma keeps a one-element list a list instead of a scalar.
        return ,$items
    }
    return $Value
}

function Get-ConfigState {
    if (-not (Test-Path -LiteralPath $script:StateFile -PathType Leaf)) {
        return @{ version = 1; entries = @{} }
    }
    $raw = Get-Content -LiteralPath $script:StateFile -Raw -ErrorAction Stop
    $parsed = $raw | ConvertFrom-Json -ErrorAction Stop
    $entries = @{}
    if ($null -ne $parsed.entries) {
        foreach ($property in $parsed.entries.PSObject.Properties) {
            $entries[$property.Name] = ConvertTo-Hashtable -Value $property.Value
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
        $State.entries[$Entry] = @{ values = @(); lsa = @(); power = @(); bcd = @(); applied = @{} }
    }
    $record = $State.entries[$Entry]
    foreach ($field in @('values', 'lsa', 'power', 'bcd')) {
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
        previous = Get-PowerSetting -Setting $Setting
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

function Add-PreviousBcd {
    param([hashtable]$Record, [string]$Name)
    foreach ($existing in $Record.bcd) {
        if ($existing.name -eq $Name) { return }
    }
    $current = Read-BcdValue -Name $Name
    $Record.bcd += @{ name = $Name; existed = [bool]$current.existed; previous = $current.value }
}

# -- live view ------------------------------------------------------------

# The live view reports what is on the guest and nothing else: raw registry
# values, raw BCD tokens and the two memory figures. Whether a value means
# "test signing is on" or whether the pagefile is big enough for a complete
# dump is decided on the host, in transport/guest_config.py, so the same facts
# can be tested without a guest and one reading of them exists.
function Get-DumpCapacity {
    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    $pageBytes = [int64]0
    foreach ($page in @(Get-CimInstance -ClassName Win32_PageFileUsage -ErrorAction SilentlyContinue)) {
        $pageBytes += [int64]$page.AllocatedBaseSize * 1MB
    }
    $memoryBytes = [int64]$os.TotalVisibleMemorySize * 1KB
    return @{
        memory_bytes = $memoryBytes
        pagefile_bytes = $pageBytes
    }
}


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
    $testSigning = Read-BcdValue -Name 'testsigning'
    $debugEntry = Read-BcdValue -Name 'debug'
    $dumpMode = Read-RegistryValue -Path $crashControlKey -Name 'CrashDumpEnabled'
    $hvci = Read-RegistryValue -Path $hvciKey -Name 'Enabled'
    $vbs = Read-RegistryValue -Path $deviceGuardKey -Name 'EnableVirtualizationBasedSecurity'
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
        no_lock_screen = $lockScreen.value
        disable_cad = $cad.value
        consent_prompt_behavior_admin = $uac.value
        enable_lua = $lua.value
        no_auto_update = $update.value
        no_auto_reboot_with_logged_on_users = $reboot.value
        screen_save_active = $screenSave.value
        power = @{
            standby_timeout_ac = Get-PowerSetting -Setting 'STANDBYIDLE'
            monitor_timeout_ac = Get-PowerSetting -Setting 'VIDEOIDLE'
            console_lock = Get-PowerSetting -Setting 'CONSOLELOCK'
        }
        interactive_sessions = $sessions
        boot = @{
            testsigning = $testSigning.token
            debug = $debugEntry.token
            crash_dump_enabled = $dumpMode.value
            hvci_enabled = $hvci.value
            vbs_enabled = $vbs.value
        }
        dump_capacity = Get-DumpCapacity
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

# -- driver-lab entries ---------------------------------------------------


function Set-TestSigning {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([hashtable]$Record)
    if (-not $PSCmdlet.ShouldProcess('bcdedit testsigning', 'Enable test signing')) { return }
    Add-PreviousBcd -Record $Record -Name 'testsigning'
    Set-BcdValue -Name 'testsigning' -Value 'on'
    $Record.applied = @{ testsigning = 'on' }
}

function Set-KernelDebug {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([hashtable]$Record)
    if (-not $PSCmdlet.ShouldProcess('bcdedit debug', 'Enable kernel debugging')) { return }
    # The transport (serial or KDNET) is configured separately; this only turns
    # the boot entry on.
    Add-PreviousBcd -Record $Record -Name 'debug'
    Set-BcdValue -Name 'debug' -Value 'on'
    $Record.applied = @{ kernel_debug = 'on' }
}

function Set-CrashDump {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([hashtable]$Record, [string]$Mode)
    if (-not $PSCmdlet.ShouldProcess('CrashControl', "Set the dump mode to $Mode")) { return }
    foreach ($name in @('CrashDumpEnabled', 'AutoReboot', 'Overwrite', 'AlwaysKeepMemoryDump')) {
        Add-PreviousValue -Record $Record -Path $crashControlKey -Name $name
    }
    $enabled = switch ($Mode) {
        'none' { 0 }
        'complete' { 1 }
        'kernel' { 2 }
        default { throw "Unsupported crash dump mode: $Mode" }
    }
    Set-RegistryValue -Path $crashControlKey -Name 'CrashDumpEnabled' -Value $enabled -Kind 'DWord'
    Set-RegistryValue -Path $crashControlKey -Name 'AutoReboot' -Value 1 -Kind 'DWord'
    Set-RegistryValue -Path $crashControlKey -Name 'Overwrite' -Value 1 -Kind 'DWord'
    Set-RegistryValue -Path $crashControlKey -Name 'AlwaysKeepMemoryDump' -Value 1 -Kind 'DWord'
    $Record.applied = @{ crash_dump = $Mode; crash_dump_enabled = $enabled }
}

function Set-HvciOff {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([hashtable]$Record)
    if (-not $PSCmdlet.ShouldProcess('Device Guard', 'Disable HVCI and VBS')) { return }
    Add-PreviousValue -Record $Record -Path $deviceGuardKey -Name 'EnableVirtualizationBasedSecurity'
    Add-PreviousValue -Record $Record -Path $hvciKey -Name 'Enabled'
    Set-RegistryValue -Path $deviceGuardKey -Name 'EnableVirtualizationBasedSecurity' -Value 0 -Kind 'DWord'
    Set-RegistryValue -Path $hvciKey -Name 'Enabled' -Value 0 -Kind 'DWord'
    $Record.applied = @{ hvci_enabled = 0; vbs_enabled = 0 }
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
    $outcome = Invoke-ToggleEntry -State $state -Name 'testsigning' -Setting $settings.testsigning `
        -Apply { param($record) Set-TestSigning -Record $record }
    if ($outcome) { $changed += $outcome }
    $outcome = Invoke-ToggleEntry -State $state -Name 'kernel_debug' -Setting $settings.kernel_debug `
        -Apply { param($record) Set-KernelDebug -Record $record }
    if ($outcome) { $changed += $outcome }
    $outcome = Invoke-ToggleEntry -State $state -Name 'hvci' -Setting $settings.hvci `
        -Apply { param($record) Set-HvciOff -Record $record }
    if ($outcome) { $changed += $outcome }

    # crash_dump is a mode, not a toggle: false restores the previous mode and a
    # string applies kernel, complete, or none.
    if ($null -ne $settings.crash_dump) {
        if ($settings.crash_dump -eq $false) {
            if ($state.entries.ContainsKey('crash_dump')) {
                [void](Restore-Entry -Record $state.entries['crash_dump'])
                $state.entries.Remove('crash_dump')
            }
            $changed += 'crash_dump:off'
        } else {
            $record = Get-EntryRecord -State $state -Entry 'crash_dump'
            Set-CrashDump -Record $record -Mode ([string]$settings.crash_dump)
            $changed += "crash_dump:$($settings.crash_dump)"
        }
    }

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
    foreach ($item in $Record.bcd) {
        if ($item.existed) {
            Set-BcdValue -Name $item.name -Value ([string]$item.previous)
        } else {
            Remove-BcdValue -Name $item.name
        }
        $restored += $item.name
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

# Dot-sourcing this file loads the functions and runs nothing, which is how the
# state-file test drives them against a temporary file without touching a guest.
if ($MyInvocation.InvocationName -ne '.') {
    try {
        if ($Mode -notin @('Apply', 'Status', 'Revert')) {
            throw "-Mode must be Apply, Status or Revert; got '$Mode'."
        }
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
}
