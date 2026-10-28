# hyperv-console-mcp

A local, policy-gated [Model Context Protocol](https://modelcontextprotocol.io/) server for **Hyper-V on Windows**. It combines VM management, checkpoints, PowerShell Direct diagnostics, synthetic keyboard/mouse input, experimental VMConnect capture, installation media, serial consoles, and serial WinDbg preparation in one stdio server.

**Alpha status:** 37 unit/transport tests and a real MCP stdio client smoke test pass, including a local Windows named-pipe transport test. The package builds as a wheel. Live Hyper-V reads currently return a clear access-denied error from the unelevated development shell; successful live management still needs an elevated/authorized host validation. No driver, mapper, or guest payload is bundled or executed by this project.

## Highlights

- **38 MCP tools** with read-only/destructive annotations and structured results.
- Inventory, pause/resume, bounded state waiting, power, checkpoints, and bounded guest file transfer.
- Guest OS health, processes, services, and recent event logs through PowerShell Direct.
- Hyper-V `Msvm_Keyboard` and `Msvm_SyntheticMouse`, explicit key/button states, and ordered console batches: works without guest networking.
- Persistent local PowerShell worker, avoiding process/module startup on every request.
- COM1/named-pipe WinDbg preparation with separate host and guest phases, and no automatic shutdown or reboot.
- Dedicated serial reader with bounded retained output and a persistent local log.
- Existing DVD-drive ISO mount/eject and optional boot-first selection while the VM is Off.
- Exact confirmation gates for forced power, restore/delete, debug preparation, media changes, and overwrite.
- No guest password in tool arguments, command lines, or audit records.
- A replaceable backend and tests that never mutate live VMs.

## Requirements

- Windows 10/11 Pro/Enterprise or Windows Server with the Hyper-V management platform.
- Windows PowerShell 5.1 and the Hyper-V PowerShell module.
- Python 3.11+ with `mcp>=2.3,<3` and Pillow.
- Hyper-V management permission (elevated host or suitably authorized account).
- Windows guest with PowerShell Direct for guest command/file tools.
- Visible interactive VMConnect window for experimental capture; no network dependency for console input.

## Install

From the repository root, using a virtual environment is recommended:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m pytest
```

Set the protected environment before launching the MCP host. Start read-only:

```powershell
$env:HYPERV_MCP_ALLOWED_VMS = 'NORTH-Test'
$env:HYPERV_MCP_ALLOW_POWER = 'false'
$env:HYPERV_MCP_ALLOW_CHECKPOINTS = 'false'
$env:HYPERV_MCP_ALLOW_GUEST = 'false'
$env:HYPERV_MCP_ALLOW_CONSOLE = 'false'
$env:HYPERV_MCP_ALLOW_SCREENSHOTS = 'false'
$env:HYPERV_MCP_ALLOW_DEBUGGER = 'false'
$env:HYPERV_MCP_ALLOW_MEDIA = 'false'
hyperv-console-mcp
```

See [client configuration](docs/SETUP.md) for a stdio JSON example. All mutations require an explicit VM allowlist and their own capability flag. A `.env` file is **not** loaded automatically.

## Tool groups

| Group | Tools |
|---|---|
| Host/VM | `hyperv_host_status`, `vm_list`, `vm_get`, `vm_wait`, `vm_power` |
| Checkpoints | `checkpoint_list`, `checkpoint_create`, `checkpoint_restore`, `checkpoint_delete` |
| PowerShell Direct | `guest_status`, `guest_processes`, `guest_services`, `guest_events`, `guest_exec`, `guest_read_file`, `guest_write_file` |
| Console | `console_info`, `console_type_text`, `console_press_keys`, `console_key_state`, `console_mouse_buttons`, `console_batch`, `console_ctrl_alt_delete`, `console_mouse_move`, `console_click`, `console_scroll`, `console_screenshot` |
| Media/boot | `media_list`, `media_mount`, `media_eject` |
| Serial console | `serial_status`, `serial_connect`, `serial_read`, `serial_send`, `serial_disconnect` |
| Kernel debugging | `debug_serial_status`, `debug_serial_prepare`, `debug_guest_prepare` |

Power actions are `start`, `shutdown`, `save`, `pause`, `resume`, `turn_off`, and `reset`. Forced power needs `confirm_vm` equal to `vm_name`. Restore/delete need the VM name and exact checkpoint GUID confirmed. Overwriting an existing guest file needs `overwrite=true` and an exact `confirm_path`.

Media operations use an existing DVD drive and require the VM to be Off. `media_mount(..., boot_first=true)` changes firmware/BIOS boot priority; it does not start the VM. Serial attachment requires an already configured COM1 named pipe and a Running VM. It is not a WinDbg replacement and cannot be combined with a debugger that already owns that pipe.

## Performance and debugging

The server reuses one local PowerShell worker. The fake-worker benchmark measures startup and warm requests without Hyper-V; it is not presented as a live VM benchmark. PowerShell Direct remains the default for isolated guests. Optional SSH is appropriate only for frequent guest administration on an intentionally configured isolated network.

For kernel work, choose **KDNET** after reviewing a suitable network path, or **COM1/named pipe** to preserve a no-network VM. Guest BCD and host COM1 preparation are separate, explicitly confirmed operations. Neither stops nor reboots the VM automatically.

- [Performance rationale and benchmarks](docs/PERFORMANCE.md)
- [WinDbg, KDNET, and serial setup](docs/DEBUGGING.md)

## Security boundaries

- Local stdio only; no HTTP server or unauthenticated network listener.
- Read-only mode by default; allowlist required for every mutation.
- Console input is real input to the VM and can change state immediately.
- Screenshots may contain sensitive guest information and are gated separately.
- Audit events contain timestamp, operation, VM, outcome, and error code only. Guest commands, typed text, passwords, and file contents are excluded.
- A guest-command timeout may leave that command running; inspect before retrying.
- Checkpoint restoration and deletion can discard data; the server does not make them implicit recovery steps.
- This project does not manage BYOVD, load drivers, or treat debugger configuration as authorization to run a payload.

See [security policy](SECURITY.md) and [project journal](docs/PROJECT.md) for implementation decisions and current limitations.

## Development

```powershell
python -m pytest
python -m build
```

The PowerShell bridge accepts fixed operations and JSON over stdin, not generated PowerShell source. The fake bridge allows transport tests without Hyper-V. Live testing must be separately authorized and restricted to a disposable VM.

## License

MIT — see [LICENSE](LICENSE).
