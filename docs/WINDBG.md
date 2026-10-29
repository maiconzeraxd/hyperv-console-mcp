# Kernel debugging: kd.exe, KDNET and the COM1 pipe

This server drives the classic command-line debugger — `kd.exe` for kernels, `cdb.exe` for
user mode — over its stdio. WinDbgX (the Store app) has no stable stdio protocol, so it is
never a candidate; the interactive WinDbg window is still useful for hands-on work, but the
`kd_*` tools do not talk to it.

The debugger is found in this order: `HYPERV_MCP_DEBUGGERS_DIR`, then `PATH`, then the
Windows SDK layouts (`Windows Kits\10` and `Windows Kits\11`, `Debuggers\x64`). If none of
them has `kd.exe`, the tool fails with instructions instead of guessing.

## Choosing a transport

| | KDNET | COM1 named pipe |
|---|---|---|
| Guest network | one adapter on the internal `HyperV-MCP-Lab` switch | none |
| Host change | one firewall rule for `kd.exe`, one UDP port | a COM1 pipe on the VM |
| VM state while preparing | Running (BCD is set over PowerShell Direct) | Off for the pipe, Running for the guest BCD |
| Best for | frequent kernel work, fast transfers, dump-less bugchecks | a VM that must stay off the network |
| Prepared by | `debug_setup(transport="kdnet", action="prepare")` | `debug_setup(transport="serial", action="prepare")` for the pipe, `action="guest"` for the guest BCD |

SSH is a guest-administration transport, not a kernel-debug one: it cannot see the guest
before the network stack is up, so it is useless for boot debugging, breakpoints and
bugchecks.

## The session model

One `kd.exe` process per VM, owned by the server:

- **States**: `disconnected → waiting_for_target → running ↔ broken`. `kd_status` reports the
  current one, plus the log path and the transport. `running` means the target is executing;
  `broken` means the debugger prompt is live and commands are accepted.
- **Command framing**: every command is bracketed by a per-session random nonce and a
  monotonic sequence (`.echo HVMCPKD<nonce><seq>` … `.echo …END`). The end of a command is
  decided by that marker, never by sleeping and hoping. Output between the markers is the
  command's own; the rest stays in the session buffer.
- **Go-class commands** (`g`, `gh`, `gn`, `gc`, `gu`) cannot use a trailing marker — the
  debugger returns when the target stops. They are recognised by their head and return when
  the prompt reappears.
- **Break-in** sends `CTRL_BREAK` to the debugger's own process group (SIGINT off Windows).
  If the console break does not stop the target, `kd_execution(action="break")` falls back to
  `vm_inject_nmi` (`Debug-VM -InjectNonMaskableInterrupt`), which needs
  `HYPERV_MCP_ALLOW_DEBUGGER` and `confirm_vm`. Pass `nmi=false` to disable the fallback.
- **Logging**: everything `kd.exe` prints is appended to
  `%LOCALAPPDATA%\HyperVConsoleMCP\kd\<vm>\<timestamp>.log`. Tool results are truncated to
  `HYPERV_MCP_MAX_OUTPUT_CHARS` with an explicit marker; the log keeps the whole thing.
- **One owner per pipe**: `kd_attach` over serial detaches a `serial_connect` reader first
  and reports `serial_detached`, and `serial_connect` refuses while a kd session owns the
  pipe.

## Tools

Setup (all gated by `HYPERV_MCP_ALLOW_DEBUGGER`; `*_prepare`/`*_deprovision` also need the
exact `confirm_vm`):

| Tool | What it does |
|---|---|
| `debug_status(transport="serial")` | COM1 pipe and VM state, read-only |
| `debug_status(transport="kdnet")` | prepared state, port, host IP, firewall rule, key present (never the key) |
| `debug_setup(transport="serial", action="guest")` | sets `bcdedit /dbgsettings SERIAL DEBUGPORT:1 BAUDRATE:115200` in the guest, checks Secure Boot first |
| `debug_setup(transport="serial", action="prepare")` | creates the host-side COM1 pipe while the VM is Off |
| `debug_setup(transport="kdnet", action="prepare")` | internal switch, UDP port 50000–50039, guest BCD, host firewall rule, key |
| `debug_setup(transport="kdnet", action="deprovision")` | removes the rule, the key and the state file |
| `vm_inject_nmi` | the break-in fallback |

Session:

| Tool | Notes |
|---|---|
| `kd_attach` | `transport="kdnet"` (default) or `"serial"`; `break_on_connect`, `initial_break` |
| `kd_status` | state, transport, log path; never the command line |
| `kd_execution` | `action="break"` (console break, NMI fallback) or `action="go"` with `g`/`gh`/`gn`/`gc`/`gu`, which returns when the target stops |
| `kd_command` | one line of debugger syntax; result carries the SHA-256 of the whole command |
| `kd_wait_event` | waits for a break or a bugcheck, reports the module that loaded |
| `kd_breakpoint` | `action="set"` for `bp`/`bu` (`deferred=true`), `action="clear"` for `bc *`, `action="on_load"` for `sxe ld:<module>` / `sxd ld:<module>` |
| `kd_inspect(what="stack")` | `k <depth>` |
| `kd_inspect(what="modules")` | `lm` |
| `kd_inspect(what="analyze")` | `!analyze -v`, parsed |
| `kd_inspect(what="dump", dump_path=…)` | opens a `.dmp` under the evidence root and runs `!analyze -v` |

`kd_command`, `kd_execution` and `kd_breakpoint` are annotated destructive: debugger syntax can
write guest memory and registers. `kd_status`, `kd_inspect` and `kd_wait_event` do not change
the target and are read-only.

## KDNET setup

`debug_setup(transport="kdnet", action="prepare", vm_name, confirm_vm, port=None,
use_existing_adapter=False)`:

1. Verifies the guest answers PowerShell Direct before touching anything.
2. Creates the internal switch `HyperV-MCP-Lab` and the host address if they are missing
   (never an external switch), and attaches an adapter to the target VM unless
   `use_existing_adapter` is set.
3. Allocates the lowest free UDP port in 50000–50039 across the prepared VMs.
4. Generates the key on the host — four octets in 1..255 — writes it into the guest BCD over
   PowerShell Direct, and enables debug. The guest echoes its `dbgsettings` back **masked**.
5. Stores the key in the Windows Credential Manager under `HyperVConsoleMCP/kdnet/<vm>` and
   adds one inbound UDP firewall rule named `HyperVConsoleMCP KDNET <vm>`, scoped to that
   `kd.exe`, that port and that interface.

The key never appears in a tool result, an event, an audit record, or the state file. It is
put on `kd.exe`'s command line only when a session starts, because KDNET requires it there.

**The guest must reboot before the setting takes effect.** The tool does not reboot it. After
the reboot, `kd_attach` connects. `debug_setup(transport="kdnet", action="deprovision")` removes the firewall rule, the
Credential Manager entry and the state file; it turns debug off in the guest when the guest is
reachable, and reports `guest.success=false` when it is not.

## Serial setup

`debug_setup(transport="serial", action="guest")` sets the guest BCD over PowerShell Direct
(Secure Boot is checked first, and a failure leaves BCD untouched).
`debug_setup(transport="serial", action="prepare")` creates the host COM1 pipe while the VM is Off. The guest reboots deliberately, then `kd_attach(transport="serial")`
builds:

```
kd.exe -k com:pipe,port=\\.\pipe\<name>,baud=115200,reconnect,resets=0
```

The VM must be Running for that attach.

## Symbols

`_NT_SYMBOL_PATH` is built for every session as
`srv*<HYPERV_MCP_SYMBOL_ROOT>*https://msdl.microsoft.com/download/symbols`, plus the directory
of any staged lab package that ships a `.pdb`. The Microsoft symbol server is the source of
truth; the local cache keeps repeated attaches fast and works offline once a module has been
seen. A private symbol store can be added by pointing `HYPERV_MCP_SYMBOL_ROOT` at a directory
that already holds the modules you care about.

## Bugcheck flow

1. `kd_wait_event` blocks until the target breaks; a bugcheck comes back as
   `{"event": "bugcheck"}` instead of a normal break.
2. `kd_inspect(what="analyze")` runs `!analyze -v` and parses it. The report carries `bugcheck_code`,
   `bugcheck_code_int`, `bugcheck_parameters`, `blamed_driver`, `probably_caused_by`,
   `module_name`, `symbol_name`, `stack_frames` and `top_frames`. Addresses lose the
   debugger's backtick; a text that is not an analysis comes back with `found: false` rather
   than a fabricated code.
3. `lab_cycle(..., debugger="kdnet"|"serial")` does this for you: it attaches kd before the
   run, arms `sxe ld:<driver>` for every `.sys` the package manifest lists, waits for an
   event afterwards, and when the guest bugchecked it reports
   `outcome="bugcheck"` with the parsed analysis in `debugger.analysis` — instead of
   classifying the dropped session as a hang.

Opening a saved dump works the same way without a live target: `kd_inspect(what="dump",
dump_path=...)` accepts only a `.dmp` under `HYPERV_MCP_EVIDENCE_ROOT`, runs `!analyze -v` and
returns the same report shape.

## Troubleshooting

- **`waiting_for_target` never changes**: the guest did not reboot after KDNET preparation, or
  it booted with the debug setting off. Check `debug_status(transport="kdnet")`, then reboot.
- **`kd_execution(action="break")` does not stop the target**: the console break is not reaching
  the debugger. The
  NMI fallback needs a guest that handles NMI; `nmi=false` disables it when you would rather
  see the original error.
- **`Could not find kd.exe`**: install the Windows SDK "Debugging Tools for Windows" (x64) or
  set `HYPERV_MCP_DEBUGGERS_DIR`.
- **Serial attach fails**: the VM is not Running, or COM1 has no debugger pipe.
  `debug_setup(transport="serial", action="prepare")` needs the VM Off, and `serial_connect`
  refuses while kd owns the pipe.

## References

- [Setting Up Network Debugging of a Virtual Machine with KDNET](https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/setting-up-network-debugging-of-a-virtual-machine-host)
- [Hyper-V `Set-VMComPort`](https://learn.microsoft.com/en-us/powershell/module/hyper-v/set-vmcomport)
- [BCDEdit `/debug`](https://learn.microsoft.com/en-us/windows-hardware/drivers/devtest/bcdedit--debug)
- [`!analyze` extension](https://learn.microsoft.com/en-us/windows-hardware/drivers/debuggercmds/-analyze)
