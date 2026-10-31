# MCP client configuration

## Install on a new machine

On a machine without Python, start with the release: `HyperVConsoleMCP-Setup-<version>.exe`
from the [releases page](https://github.com/maiconzeraxd/hyperv-console-mcp/releases), or
`winget install maiconzeraxd.HyperVConsoleMCP`. Both install per user into
`%LOCALAPPDATA%\Programs\Hyper-V Console`, ask for no elevation during the install, and run the routine below once
from the installer's last page — so on that path `hyperv-setup` is the installer's own step, and
`hyperv-setup --check` is what you run to look without installing. The bundle is four programs
beside each other: `HyperVConsoleMCP.exe` (the window, and the server when a client gives it a
pipe), `hyperv-setup.exe`, `hyperv-service.exe` and `hyperv-access.exe`. The window always runs
elevated; the installer and the operator tools do not have to.

One routine does the whole install, and every path into it ends in the same place:

```powershell
hyperv-setup --check     # what this host needs; changes nothing, exit 0 means go
hyperv-setup             # write the client config, add the shortcut, run the self-test
```

`--check` answers three questions and prints one sentence per answer: which edition of Windows this is (Home cannot run Hyper-V at all), whether Hyper-V is installed and switched on, and — from `hyperv-setup` rather than the check — which MCP clients are already on the machine. When the feature is installed but off, the routine stops with the exact command that turns it on (`Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V -All`); it never reboots a machine and never asks for a restart itself.

The client config is written into each client that is present — Claude Desktop, Cursor and Codex, detected by their own folder, not by a config file that a fresh install does not have yet. On a machine with none of them the routine prints the entry and the file each client keeps it in, instead of leaving the operator to read the source. `--uvx`, `--profile safe|lab` and `--no-shortcut` behave as they do for `hyperv-install`, which remains the low-level command that writes one client's file and nothing else.

The install is idempotent: a second run reports `unchanged` for every file it already wrote, and repairs a partial install rather than repeating it. The last step asks the service and Hyper-V whether they answer, and names the failing step when they do not.

What the install deliberately leaves to the window is the part that needs a human decision: which VM, what the AI may do with it, and the guest password. A host where no VM has a level opens the dashboard on a three-question wizard for exactly that, and stops offering it once one VM has a level. It writes the same `access.json` `hyperv-access` writes and stores the password through the service's `guest_credential_set`, so nothing about the policy changes — see [DASHBOARD.md](DASHBOARD.md#the-first-run).

`scripts/install.ps1` is the same routine with the pre-check in front of it, for a host where the package is installed but nothing has been configured:

```powershell
.\scripts\install.ps1 -Check
.\scripts\install.ps1
.\scripts\install.ps1 -Uninstall
```

It asks before removing the saved guest credentials or the state folder — `-Uninstall` alone keeps both, because a guest password and a run's evidence are the operator's data, not the installer's. `-RemoveCredentials` and `-RemoveData` answer the two questions without asking. The script is not recommended as a first install: downloading a script and running it as an administrator is a habit this tool should not teach, and the signed installer runs the same routine.

## Profiles

Create a protected environment for the MCP host. Do not commit real passwords.

```powershell
$env:HYPERV_MCP_ALLOWED_VMS = 'NORTH-Test'
$env:HYPERV_MCP_ALLOW_POWER = 'false'
$env:HYPERV_MCP_ALLOW_CHECKPOINTS = 'false'
$env:HYPERV_MCP_ALLOW_GUEST = 'false'
$env:HYPERV_MCP_ALLOW_CONSOLE = 'false'
$env:HYPERV_MCP_ALLOW_SCREENSHOTS = 'false'
$env:HYPERV_MCP_ALLOW_DEBUGGER = 'false'
$env:HYPERV_MCP_ALLOW_MEDIA = 'false'
```

For PowerShell Direct only, additionally set `HYPERV_MCP_GUEST_USERNAME` and `HYPERV_MCP_GUEST_PASSWORD` in the protected host environment. They are not command-line arguments or tool parameters.

## Profiles

`HYPERV_MCP_PROFILE=safe` (the default) keeps every capability off until its own `HYPERV_MCP_ALLOW_*` variable is set. `HYPERV_MCP_PROFILE=lab` enables all seven capabilities for the allowlisted VMs and stops asking the dashboard for approval, so an autonomous loop does not wait for a click on every step:

```powershell
$env:HYPERV_MCP_PROFILE = 'lab'
$env:HYPERV_MCP_ALLOWED_VMS = 'NORTH-Test'
```

What the lab profile does **not** change: the allowlist (an empty `HYPERV_MCP_ALLOWED_VMS` still refuses every mutation), the `confirm_*` fields, the confined host paths, the ban on host driver/payload execution, and the audit and event records, which keep every operation. An explicit `HYPERV_MCP_ALLOW_<CAPABILITY>=false` still wins, so one capability can be held back inside the profile. The active profile is reported by `hyperv_host_status` and shown in the dashboard sidebar.

`lab_run` and `lab_cycle` — which execute a staged package after verifying it and creating checkpoint `pre-<run_id>` — are refused in the safe profile. The approval phrase the guest runner demands is generated by the server, so setting the profile is the operator's decision to let a loop run packages.

## Guest credential

The primary place for the guest password is the current user's Windows Credential Manager, under the generic target `HyperVConsoleMCP/<vm>`. Set it from the dashboard's **Guest access** page (typed locally, written straight into the vault) or with `guest_credential_set` from a Python session. It is deliberately not an MCP tool, so the AI never types the password. `guest_credential(action="rotate")` generates a new random password, sets it in the guest, proves a fresh session accepts it, and only then stores it, restoring the previous password if the guest rejects the new one. `guest_credential_status` reports where the credential came from and whether PowerShell Direct and SSH accept it, and never returns the secret.

`HYPERV_MCP_GUEST_USERNAME` and `HYPERV_MCP_GUEST_PASSWORD` keep working as a fallback for existing setups and win nothing over a stored entry; the status tool reports `environment` so the weaker source is visible.

Example host configuration using Python directly:

```json
{
  "mcpServers": {
    "hyperv": {
      "command": "C:\\Path\\To\\Python\\python.exe",
      "args": [
        "-I",
        "C:\\Path\\To\\hyperv-console-mcp\\scripts\\run_server.py"
      ],
      "env": {
        "HYPERV_MCP_ALLOWED_VMS": "NORTH-Test",
        "HYPERV_MCP_ALLOW_POWER": "false",
        "HYPERV_MCP_ALLOW_CHECKPOINTS": "false",
        "HYPERV_MCP_ALLOW_GUEST": "false",
        "HYPERV_MCP_ALLOW_CONSOLE": "false",
        "HYPERV_MCP_ALLOW_SCREENSHOTS": "false",
        "HYPERV_MCP_ALLOW_DEBUGGER": "false",
        "HYPERV_MCP_ALLOW_MEDIA": "false"
      }
    }
  }
}
```

After an editable/package installation, `hyperv-console-mcp` can be the command instead. It runs the server on stdio — it never listens on TCP — and, run by a person rather than by a client, the same command opens the window: an MCP client always hands the process a pipe, so a pipe gets the server, while a terminal or the Start Menu shortcut gets the window. `--stdio` and `--ui` say which one you meant.

`hyperv-install --client claude` (also `codex` and `cursor`) writes that entry for you: it merges one `mcpServers.hyperv` entry into the client's own file, copies the file first, refuses one it cannot parse instead of rewriting it, and appends a single `[mcp_servers.hyperv]` section to Codex's TOML. `--uvx` writes `uvx hyperv-console-mcp` instead of the installing interpreter, `--profile lab` sets `HYPERV_MCP_PROFILE`, and `--shortcut` adds a **Hyper-V Console** Start Menu entry that opens the window. The command is not an MCP tool; nothing the AI can call reaches it.

The notification-area icon is a separate optional extra — `pip install "hyperv-console-mcp[tray]"`, then `hyperv-tray` — and the routine does not start it. See [the dashboard guide](DASHBOARD.md#the-notification-area-icon).

The process must have Hyper-V management permission. Run the MCP host elevated or grant the intended account Hyper-V Administrators access through your normal administrative process. The server never changes host group membership or UAC settings.

### "Hyper-V access denied" — the one-time permission fix

Being a local administrator is not enough. A standard (non-elevated) process has its administrator
group filtered out by UAC, so unless the account is also in **Hyper-V Administrators** every call
that touches the host — `hyperv_host_status`, `vm_power`, checkpoints, media, and PowerShell Direct
— comes back as:

```
Hyper-V access denied. Run the MCP host elevated or grant Hyper-V Administrators access.
```

The group is what grants access without elevation, and it is the shape the roadmap asks for:
**elevation happens once at install, not per action.** In an elevated PowerShell, once:

```powershell
# "Hyper-V Administrators" on an English Windows;
# "Administradores do Hyper-V" on a Portuguese one.
Add-LocalGroupMember -Group 'Administradores do Hyper-V' -Member "$env:COMPUTERNAME\$env:USERNAME"
```

Then **sign out and back in** (or reboot): a group added to the account is not in the token the
session already holds, so the process that is running keeps failing until the next logon. Restart
any service host that was already running for the same reason — it is a client of the same token.

Check it without opening anything:

```powershell
# A non-elevated PowerShell, after the new logon: this must answer, not refuse.
$env:PYTHONPATH = 'C:\path\to\hyperv-console-mcp\src'
python -c "from hyperv_console_mcp.config import Settings; from hyperv_console_mcp.service import HyperVService; from hyperv_console_mcp.hostwmi import create_backend; print(HyperVService(create_backend(), Settings.from_env()).host_status()['hypervisor_present'])"
```

Once it answers, nothing else needs elevation: the interface, the AI and the command line all drive
the same per-user host, which now has the rights it needs.

### "Sempre elevado" without a UAC prompt: the logon task

The group needs one new logon before it takes effect, and a machine may not have Hyper-V
Administrators at all. The other way to have the rights is for the **host** to run elevated while
every client stays ordinary — the window, the AI and the command line all reach it over the pipe.

`scripts/install_elevated_host.ps1` registers that: a scheduled task, `HyperVConsoleMCP-Host`, that
runs at logon with the **highest run level**, as the operator, launching the host under `pythonw`
(no window). The task is *Interactive* on purpose — the host reads that user's Credential Manager
and owns that user's pipe, which is why it is not a service account
([the decision is in PROJECT.md](PROJECT.md)).

```powershell
# In an elevated PowerShell, once.
.\scripts\install_elevated_host.ps1 -PythonPath 'C:\Python311\pythonw.exe' -SourceRoot 'C:\tools\hyperv-console-mcp'
Start-ScheduledTask -TaskName HyperVConsoleMCP-Host   # start it now instead of waiting for the next logon
```

Remove it the same way it was made:

```powershell
.\scripts\install_elevated_host.ps1 -Unregister
```

What it buys: after the next logon the host is already up and elevated, so no client ever asks for
UAC, and a reboot does not undo it. What it costs: a process with administrator rights running for
the whole session, owned by the operator and reachable only through that user's pipe. If you would
rather have no such process, use the group above and leave the task out — the two are alternatives,
not a sequence.

### Credentials are not the reason for the prompt

The guest password lives in the Windows Credential Manager under `HyperVConsoleMCP/<vm>`, and the
service builds the `PSCredential` from it — there is no `Get-Credential` anywhere in the package
(`tests/test_lab_worker.py` asserts it). So a credential prompt you see is not the service asking:
it is the VM's own console (`vmconnect`, where Windows asks for the guest logon) or a script written
by hand. `guest_credential_status` tells you which account the service will use and where it came
from, and never returns the secret.

SSH with a key removes the password from the guest path entirely — but provisioning it bootstraps
through PowerShell Direct, so it needs the permission above first. See
[the SSH guide](SSH.md).

## Confined host paths

ISO mounts accept only files under `HYPERV_MCP_ISO_ROOT` (default `%LOCALAPPDATA%\HyperVConsoleMCP\media`). Serial logs must remain under `HYPERV_MCP_SERIAL_LOG_ROOT` (default `%LOCALAPPDATA%\HyperVConsoleMCP\serial`). Set both roots explicitly when using a different library or protected log directory:

```powershell
$env:HYPERV_MCP_ISO_ROOT = 'C:\VMs_iso'
$env:HYPERV_MCP_SERIAL_LOG_ROOT = "$env:LOCALAPPDATA\HyperVConsoleMCP\serial"
```

## Optional SSH transport

SSH is off unless a VM has been provisioned with `guest_ssh_provision`; PowerShell Direct stays the default and needs no extra package. To use it:

```powershell
python -m pip install 'hyperv-console-mcp[ssh]'
$env:HYPERV_MCP_GUEST_TRANSPORT = 'auto'          # auto | psdirect | ssh
$env:HYPERV_MCP_SSH_SUBNET = '10.77.0.0/24'       # private lab subnet, host takes .1
$env:HYPERV_MCP_SSH_ROOT = "$env:LOCALAPPDATA\HyperVConsoleMCP\ssh"
$env:HYPERV_MCP_OPENSSH_PACKAGE = 'C:\VMs_iso\OpenSSH-Win64'   # only for guests without internet
```

Per-VM overrides use `HYPERV_MCP_GUEST_TRANSPORT_<VMNAME>` (dashes and underscores both work). `ssh` never falls back silently: if the VM is not provisioned, the call fails with instructions instead of quietly switching transport. `auto` uses SSH only when this tool provisioned the VM and the connection answers within two seconds.

Provisioning creates a dedicated **internal** switch named `HyperV-MCP-Lab` (never external), gives the host `10.77.0.1` on it, installs a key-only sshd in the guest, and pins the guest host key in `%LOCALAPPDATA%\HyperVConsoleMCP\ssh\<vm>\known_hosts`. The private key never leaves that directory, whose ACL is restricted to the current user. `HYPERV_MCP_OPENSSH_PACKAGE` is only read when the guest cannot install `OpenSSH.Server` itself; point it at an extracted Win32-OpenSSH release containing `install-sshd.ps1`.

## Kernel debugging

The `kd_*` tools need `HYPERV_MCP_ALLOW_DEBUGGER=true`, an allowlisted VM, and a classic
command-line debugger. `kd.exe` is found in `HYPERV_MCP_DEBUGGERS_DIR`, then `PATH`, then the
Windows SDK layouts:

```powershell
$env:HYPERV_MCP_ALLOW_DEBUGGER = 'true'
$env:HYPERV_MCP_DEBUGGERS_DIR = 'C:\Program Files (x86)\Windows Kits\10\Debuggers\x64'
$env:HYPERV_MCP_KD_LOG_ROOT   = "$env:LOCALAPPDATA\HyperVConsoleMCP\kd"
$env:HYPERV_MCP_SYMBOL_ROOT   = "$env:LOCALAPPDATA\HyperVConsoleMCP\symbols"
```

`debug_setup(transport="kdnet", action="prepare")` reuses the same internal
`HyperV-MCP-Lab` switch as SSH (never an
external one), allocates one UDP port in 50000–50039, sets the guest BCD over PowerShell
Direct, and adds one firewall rule named `HyperVConsoleMCP KDNET <vm>` for that `kd.exe`, that
port and that interface. The generated key goes into the Windows Credential Manager under
`HyperVConsoleMCP/kdnet/<vm>` and is masked in every result; the guest must be rebooted before
KDNET takes effect. `debug_setup(transport="kdnet", action="deprovision")` removes the
rule, the key and the state file.

See [WINDBG.md](WINDBG.md) for both transports, the session tools, symbols and the bugcheck
flow.

## Dashboard

Start the native interface from the repository with:

```powershell
python -I scripts\run_dashboard.py --vm NORTH-Test
```

For optional local approval of every AI-requested mutation, set `HYPERV_MCP_REQUIRE_DASHBOARD_APPROVAL=true` in both MCP and dashboard environments and leave the dashboard open. See [DASHBOARD.md](DASHBOARD.md).

## Reproducible lab

`HYPERV_MCP_LAB_ROOT` restricts host package sources; `HYPERV_MCP_EVIDENCE_ROOT` controls evidence downloads. `HYPERV_MCP_EVENT_DIRECTORY` selects the local event store shared by the dashboard and MCP. Do not place credentials in these paths or in `.env.example`.

See [LAB.md](LAB.md) for the non-executing staging/verification workflow and the separate manual guest runner. No staging or diagnostic step authorizes loading a driver.

## Where to go next

- [GUEST-ACCESS.md](GUEST-ACCESS.md) — autologon without a plain-text password, the no-prompt
  toggles, and how to revert them.
- [SSH.md](SSH.md) — the optional key-only SSH transport and its provisioning.
- [WINDBG.md](WINDBG.md) — KDNET and the COM1 pipe, the `kd.exe` session tools, symbols.
- [LAB.md](LAB.md) — packages, evidence and the autonomous `lab_run`/`lab_cycle`.
- [DASHBOARD.md](DASHBOARD.md) — the local UI and per-mutation approval.

