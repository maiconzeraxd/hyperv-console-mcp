# Guest access: boot to a desktop, no prompts, revertible

The synthetic console and the thumbnail screenshots work best against a guest that boots
straight to the desktop and never sits at a logon screen, a lock screen, or a UAC prompt.
This page covers the three pieces that get a disposable lab VM there, and how to put it back.

Everything here is a **guest** change. Nothing on this page touches the host's Secure Boot,
HVCI, test-signing, drivers or payloads.

## 1. The password lives in the vault, not in the environment

`HYPERV_MCP_GUEST_PASSWORD` is a fallback. The primary place for the guest credential is the
current user's Windows Credential Manager, under the generic target `HyperVConsoleMCP/<vm>`
(DPAPI-protected, readable only by that user).

- Set it from the dashboard's **Guest access** page, or with `guest_credential_set` from a
  Python session. It is deliberately not an MCP tool, so the AI never types the password.
- `guest_credential_status` reports `source` (`credential_manager` or `environment`), the user
  name, and whether PowerShell Direct and SSH accept the credential — never the secret.
- `guest_credential(vm_name, action="rotate", confirm_vm)` generates a strong random
  password, sets it in
  the guest over PowerShell Direct, proves a **fresh** session accepts it, and only then
  replaces the stored one. If the proof fails, the previous password is restored.

Keep `HYPERV_MCP_GUEST_USERNAME`/`HYPERV_MCP_GUEST_PASSWORD` only if you already depend on
them; `guest_credential_status` makes the weaker source visible.

## 2. Autologon without a plain-text password

`guest_autologon_configure(vm_name, enabled, confirm_vm, ...)` requires
`HYPERV_MCP_ALLOW_GUEST` and the exact `confirm_vm`. With `enabled=true` it:

- sets `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon`
  `AutoAdminLogon=1`, `DefaultUserName`, `DefaultDomainName` (`.` for the local account);
- stores the password as the **LSA private secret** `DefaultPassword` via
  `LsaStorePrivateData` — the same mechanism Sysinternals Autologon uses — and deletes any
  `DefaultPassword` value left in the registry;
- removes `AutoLogonCount` (a non-zero value logs on once and then stops);
- sets `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device\DevicePasswordLessBuildVersion=0`,
  without which Windows 10/11 may ignore autologon.

The account must be a **local** account, not a Microsoft account. A blank password is not the
default answer: Windows restricts accounts without a password to console logon
(`LimitBlankPasswordUse`), which is exactly what breaks PowerShell Direct and SSH.

## 3. The no-prompt toggles

The same call takes tri-state switches — `None` (or omitted) leaves the setting alone:

| Argument | Effect |
|---|---|
| `lock_screen` | `HKCU\...\Personalization\NoLockScreen=1` |
| `disable_cad` | `HKLM\...\Policies\System\DisableCAD=1` (no Ctrl+Alt+Del at logon) |
| `power` | AC profile: no standby, no screen blanking, no console lock (`powercfg`) |
| `uac` | UAC prompt behaviour |
| `windows_update` | blocks automatic update reboots |
| `restart` | reboots the guest at the end (needs `HYPERV_MCP_ALLOW_POWER`) |

The same Guest page also carries the guest-only BCD toggles — test signing, kernel debugging, crash dump and HVCI/VBS — which are recorded in the same `guest-config.json` and reverted by the same button.

Example:

```python
service.guest_autologon_configure(
    "NORTH-Test", True, "NORTH-Test",
    lock_screen=False, disable_cad=True, power=True, uac=False, windows_update=False,
)
```

## 4. Everything is recorded, and revertible

Every change appends the value it replaced to `C:\DriverLab\State\guest-config.json` — the
registry values, the LSA secret, the power settings and the BCD entries. `guest_config_status`
reads that file back (and the live values) without changing anything.

`guest_config_revert(vm_name, confirm_vm, restart=False)` restores every recorded value,
deletes the LSA secret, and reports what it restored. It is the intended way to hand the VM
back, and it works after the VM was rebooted in between.

The settings document that carries the password is written to
`C:\ProgramData\HyperVConsoleMCP\guest-config-<uuid>.json`, which the guest deletes before it
reads anything. The password is never on a command line, in a tool result, in an event, or in
the audit log.

## 5. Checking it worked

1. `guest_autologon_configure(..., restart=True)`, or reboot deliberately.
2. Wait for the VM to settle (`vm_get(wait_for=[...])`, or `console_wait_for_change`).
3. `console_screenshot(source="thumbnail")` — no VMConnect window needed. A desktop with no
   logon tile is the proof.
4. `guest_config_status` shows the applied values.

A live check on a disposable VM, outside the pytest suite:

```powershell
$env:HYPERV_MCP_LIVE_TEST_VM = 'LAB-Disposable'
python -I tests/live_guest_automation.py --configure --restart
python -I tests/live_guest_automation.py --check --save desktop.png
python -I tests/live_guest_automation.py --revert
```

## Related

- [SETUP.md](SETUP.md) — profiles, the guest credential, confined host paths.
- [SSH.md](SSH.md) — the optional key-only SSH transport.
- [WINDBG.md](WINDBG.md) — kernel debugging over KDNET or the COM1 pipe.
