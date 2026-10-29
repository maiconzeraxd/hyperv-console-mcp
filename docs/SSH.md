# Optional SSH transport

PowerShell Direct is the default guest transport and needs nothing extra. SSH exists for
guests where a persistent session and SFTP copies are worth more than the extra setup, and
for `auto` to pick when a VM was provisioned for it.

```
HYPERV_MCP_GUEST_TRANSPORT = auto | psdirect | ssh      # default: auto
```

`HYPERV_MCP_GUEST_TRANSPORT_<VMNAME>` overrides the choice per VM (dashes and underscores
both work: `HYPERV_MCP_GUEST_TRANSPORT_NORTH_TEST=ssh`).

- `psdirect` always uses PowerShell Direct.
- `ssh` never falls back silently: an unprovisioned VM is an error that tells you to run
  `guest_ssh_provision`.
- `auto` uses SSH only when **this tool** provisioned the VM **and** the connection answers
  within two seconds; otherwise PowerShell Direct. The health probe is cached for 30 seconds
  so `auto` does not pay a connection round trip on every tool call.

Every guest tool result carries `"transport": "psdirect" | "ssh"`, so a mixed environment is
never ambiguous.

## Why paramiko and not `ssh.exe`

Win32-OpenSSH has no `ControlMaster`, so `ssh.exe` cannot hold a session open across calls.
The server keeps one `paramiko.SSHClient` per VM with keepalive, and moves files over SFTP.
`paramiko` is an optional dependency: `python -m pip install 'hyperv-console-mcp[ssh]'`.
Nothing imports it unless a VM actually uses SSH.

## Provisioning

`guest_ssh_provision(vm_name, confirm_vm, use_existing_adapter=False)` requires
`HYPERV_MCP_ALLOW_GUEST` and runs over PowerShell Direct:

1. **Network.** Creates the internal switch `HyperV-MCP-Lab` if it is missing (never an
   external switch), gives the host `10.77.0.1` on it, and adds an adapter to the target VM
   unless `use_existing_adapter` is set. The guest subnet is `HYPERV_MCP_SSH_SUBNET`
   (default `10.77.0.0/24`).
2. **sshd.** Uses the guest's existing `OpenSSH.Server` when it is present (Windows Server
   2025 ships it disabled). Otherwise it tries
   `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`. A guest with no internet
   falls back to `HYPERV_MCP_OPENSSH_PACKAGE`, an extracted Win32-OpenSSH release whose
   `install-sshd.ps1` the host copies in; the release and its hash are the operator's to pin.
3. **Keys.** Generates an ed25519 pair on the host under
   `%LOCALAPPDATA%\HyperVConsoleMCP\ssh\<vm>\`, with an ACL restricted to the current user,
   and installs only the public half in the guest's `administrators_authorized_keys` (ACL:
   Administrators and SYSTEM). `PasswordAuthentication` is set to `no`.
4. **Host key pinning.** Reads `ssh_host_ed25519_key.pub` from the guest over PowerShell
   Direct and writes `%LOCALAPPDATA%\HyperVConsoleMCP\ssh\<vm>\known_hosts`. Connections use
   `RejectPolicy` with strict verification — there is no trust-on-first-use path, and a
   changed host key is a hard failure.
5. **Firewall.** One inbound TCP 22 rule in the guest, scoped to the host's address on the
   lab switch.
6. **Default shell.** `HKLM:\SOFTWARE\OpenSSH\DefaultShell` is set to `powershell.exe`, so a
   command means the same thing on both transports.
7. **Probe.** Connects once. If the guest does not answer, the state file is removed and the
   call fails, so `auto` is never left preferring a dead transport.

The private key never leaves its directory and is never returned by a tool.

## Status and teardown

`guest_ssh_status(vm_name)` is read-only: whether the VM is provisioned, the state file, the
guest-side check, and the SSH health probe. It reports paths, never key material.

`guest_ssh_deprovision(vm_name, confirm_vm)` removes the guest firewall rule, the
`authorized_keys` entry, the host-side state directory, and the VM adapter when this tool
created it. It does not remove a switch other VMs still use.

## Using it

```
guest_ssh_provision(vm_name="NORTH-Test", confirm_vm="NORTH-Test")
guest_report(vm_name="NORTH-Test")          # "transport": "ssh"
```

A live check on a disposable VM, outside the pytest suite:

```powershell
$env:HYPERV_MCP_LIVE_TEST_VM = 'LAB-Disposable'
python -I tests/live_ssh.py --deprovision
```

## When not to use it

- The guest has no network by design: keep PowerShell Direct and the COM1 debugger pipe.
- The guest is at boot or has bugchecked: SSH needs the network stack, so it is useless for
  kernel debugging. See [WINDBG.md](WINDBG.md).
- The network path is not isolated: the internal switch exists so that the only peer is the
  host. Do not attach it to an external switch.
