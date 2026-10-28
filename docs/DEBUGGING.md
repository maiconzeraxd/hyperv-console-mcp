# Kernel debugging with WinDbg

The host already has `WinDbgX.exe` and the WDK `kdnet.exe`/`VerifiedNICList.xml`. No debugger installation is required for the current host.

## Preferred choices

- **KDNET** for high-frequency kernel debugging when a reviewed, supported network path is available.
- **COM1 over a named pipe** for the currently isolated VM without adding a NIC or switch.
- SSH is a guest-administration option, not a kernel-debug transport. It cannot replace KD during boot, breakpoints, or bugchecks.

## Serial/named-pipe workflow

1. Create and verify a checkpoint before changing debug settings.
2. Shut down the guest cleanly. The MCP does not stop it for you.
3. Authorize the operation explicitly. With `HYPERV_MCP_ALLOW_DEBUGGER=true` and the VM allowlisted, call:

```json
{
  "vm_name": "NORTH-Test",
  "pipe_name": "north-test-kd",
  "confirm_vm": "NORTH-Test",
  "configure_guest": true
}
```

`configure_guest=true` uses the environment guest credentials and executes only fixed elevated guest commands equivalent to:

```bat
bcdedit /dbgsettings SERIAL DEBUGPORT:1 BAUDRATE:115200
bcdedit /debug {current} on
```

It changes the guest boot configuration. Reboot remains manual. The tool returns WinDbg arguments equivalent to:

```bat
WinDbgX.exe -k com:pipe,port=\\.\pipe\north-test-kd,baud=115200,reconnect,resets=0
```

Start WinDbg before starting the VM if early boot debugging is required. Disable Enhanced Session mode while stopped at breakpoints. A debugger does not prevent a BSOD; preserve the checkpoint, package hashes, guest logs, and crash dump.

## KDNET workflow — deliberate network change required

The Microsoft procedure uses an external virtual switch shared with the management OS, Secure Boot temporarily disabled for `kdnet.exe`, and a unique UDP port in 50000–50039. It also warns about Enhanced Session timeouts and firewall configuration.

1. Review and create the isolated/private network design before touching VM adapters. Do not automatically attach the disposable VM to an external switch.
2. Verify the Hyper-V synthetic NIC with the guest `kdnet.exe`.
3. Copy `kdnet.exe` and `VerifiedNICList.xml` to the guest, run elevated `kdnet <host-ip> <unique-port>`, and retain its generated key as a secret.
4. Start the returned `windbg -k net:...` command on the host, then reboot deliberately.

This server does not yet mutate KDNET network settings. Serial preparation is implemented first because it preserves the current no-network isolation. Do not confuse installing `kdnet.exe` on the host with having configured KDNET in the guest.

## References

- [Setting Up Network Debugging of a Virtual Machine with KDNET](https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/setting-up-network-debugging-of-a-virtual-machine-host)
- [Hyper-V `Set-VMComPort`](https://learn.microsoft.com/en-us/powershell/module/hyper-v/set-vmcomport)
- [BCDEdit `/debug`](https://learn.microsoft.com/en-us/windows-hardware/drivers/devtest/bcdedit--debug)
