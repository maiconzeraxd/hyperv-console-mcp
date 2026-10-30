# hyperv-console-mcp

A local, policy-gated [Model Context Protocol](https://modelcontextprotocol.io/) server for **Hyper-V on Windows**. It combines VM management, checkpoints, PowerShell Direct diagnostics, synthetic keyboard/mouse input, console screenshots without VMConnect, installation media, serial consoles, and serial WinDbg preparation and a live kd.exe kernel-debugging session in one stdio server.

**Alpha status:** 1036 unit/transport tests and a real MCP stdio client smoke test pass locally, including a local Windows named-pipe transport test and one assertion set that runs over both guest transports; the smoke checks each profile's tool surface, per toolset, over a real stdio client. Live validation on `NORTH-Test` covered the lab cycle, autologon, kd over COM1, SSH end to end, package staging and host-side verification over both guest transports (including a tampered file being named in the refusal), a ~100 KB `guest_exec` and a 100 KB file transfer over both guest transports, and the test-signing/debug BCD round trip on a pt-BR guest; `docs/PROJECT.md` records what was and was not exercised. The package builds as an sdist and wheel. No driver, mapper, or guest payload is bundled by this project; in the lab profile `lab_run` executes only a package the operator staged.

## Highlights

- **67 MCP tools in twelve toolsets**, with read-only/destructive annotations and structured results; a profile registers only its own toolsets, so the default surface is 30 tools rather than every schema in every conversation.
- Inventory, pause/resume, bounded state waiting, power, checkpoints, and bounded guest file transfer.
- A running VM reports its guest's computer name and OS from the Hyper-V key-value pair exchange,
  so a VM can be identified before any guest credential exists.
- Guest OS health, processes, services, and recent event logs through PowerShell Direct.
- Hyper-V `Msvm_Keyboard` and `Msvm_SyntheticMouse`, explicit key/button states, and ordered console batches: works without guest networking.
- Persistent local PowerShell worker, avoiding process/module startup on every request.
- COM1/named-pipe WinDbg preparation with separate host and guest phases, and no automatic shutdown or reboot.
- Dedicated serial reader with bounded retained output and a persistent local log.
- Existing DVD-drive ISO mount/eject and optional boot-first selection while the VM is Off.
- Exact confirmation gates for forced power, restore/delete, debug preparation, media changes, and overwrite.
- No guest password in tool arguments, command lines, or audit records.
- A native dashboard and a non-executing, hash-verified driver-lab transfer workflow.
- A replaceable backend and tests that never mutate live VMs.

## Requirements

- Windows 10/11 Pro/Enterprise or Windows Server with the Hyper-V management platform.
- Windows PowerShell 5.1 and the Hyper-V PowerShell module.
- Python 3.11+ with `mcp>=2.3,<3` and Pillow.
- Hyper-V management permission (elevated host or suitably authorized account).
- Windows guest with PowerShell Direct for guest command/file tools.
- Console screenshots come from the Hyper-V console thumbnail, so no VMConnect window or
  interactive session is needed; only `console_screenshot(source="vmconnect")` uses one.
  Console input has no network dependency.

## Install

Never used PowerShell? [docs/QUICKSTART.md](docs/QUICKSTART.md) is the install in plain
language, with the three questions the window asks on its first run.

Three ways in, in the order to consider them:

1. **The installer.** `HyperVConsoleMCP-Setup-<version>.exe` from the
   [releases page](https://github.com/maiconzeraxd/hyperv-console-mcp/releases): one download,
   no administrator prompt, and no Python — the runtime is inside it. It installs into
   `%LOCALAPPDATA%\Programs\Hyper-V Console`, then runs `hyperv-setup` once, so the client
   config, the shortcut and the self-test happen during the install.
2. **winget.** `winget install maiconzeraxd.HyperVConsoleMCP`, once the manifest has been
   accepted into `microsoft/winget-pkgs`. `packaging/winget/` holds the manifests this project
   submits and `packaging/winget/README.md` says what changes per release.
3. **From source**, below: what development and the lab use, and the only path that needs
   Python.

`scripts/install.ps1` runs the same routine for a machine where the package is already
installed. It is the advanced path and not the first one: downloading and running a script as
administrator is a habit worth not forming for a tool that runs as a service and controls
Hyper-V.

From the repository root, using a virtual environment is recommended:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m pip install -e '.[ui]'   # the WebView2 window; tkinter works without it
.\.venv\Scripts\python.exe -m pytest
```

Set the protected environment before launching the MCP host. The shortest path is one access level per VM, written by you — never by a tool — into `%LOCALAPPDATA%\HyperVConsoleMCP\access.json`:

```powershell
hyperv-access set "NORTH-Test" lab      # read-only | control | lab
hyperv-access list                      # what each VM may do, and the file in use
```

A level is also the allowlist: a VM with no level is not reachable. `read-only` lists and reads the VM and changes nothing; `control` adds power, checkpoints, console input, screenshots and media; `lab` adds guest access and the debugger. The same choice is on the dashboard's Overview page, and both writers tell a running service to re-read the file, so a level takes effect without a restart. The equivalent environment variables still work and are what a test or a one-off run should use:

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

A VM with a level takes its capabilities from that level — per VM is more specific than a host-wide flag — and a VM without one keeps the older behaviour, where the profile and the `HYPERV_MCP_ALLOW_*` variables decide for every VM at once. `HYPERV_MCP_ALLOWED_VMS` narrows the file rather than replacing it.

See [client configuration](docs/SETUP.md) for a stdio JSON example. All mutations require an explicit VM allowlist and their own capability flag. A `.env` file is **not** loaded automatically.

`hyperv-install` writes that entry for you, into the client's own file, and copies the file first:

```powershell
hyperv-install --client claude                 # also: codex, cursor; repeat --client for more
hyperv-install --client claude --uvx           # `uvx hyperv-console-mcp` instead of this interpreter
hyperv-install --client claude --profile lab --shortcut   # plus a Start Menu shortcut to the window
```

It merges one `mcpServers.hyperv` entry and leaves every other server and setting untouched, appends one `[mcp_servers.hyperv]` section to Codex's TOML rather than re-serialising it, and refuses — without writing — a file it cannot parse. Running it twice reports `unchanged` instead of rewriting.

The whole install is one routine, and it starts by asking the machine whether it can run this at all:

```powershell
hyperv-setup --check     # edition, Hyper-V state; changes nothing, exit 0 means go
hyperv-setup             # every detected client's config, the shortcut, the self-test
hyperv-setup --uninstall # takes the entry back out; keeps your credentials and data unless asked
```

`--check` refuses Windows Home in one sentence instead of failing inside a feature install, and stops with the exact `Enable-WindowsOptionalFeature` line when Hyper-V is installed but switched off. `scripts/install.ps1` is the same routine for a machine where the package is already present, with `-Check` and `-Uninstall`; it is documented as the advanced path, not the first one.


`HYPERV_MCP_PROFILE` picks the default toolset list (`safe` or `lab`); `HYPERV_MCP_TOOLSETS` overrides it with a comma-separated list such as `core,console,guest` or `all`. An unknown name is refused at startup rather than silently ignored.

## Zero configuration

Nothing about a particular VM is built into the program, and the environment above is the
narrowing, not the setup:

- **The VMs come from Hyper-V.** `vm_list` reads them; a running VM also reports its guest's
  computer name and OS from the key-value pair exchange, so it can be identified before any guest
  credential exists. The window opens the only VM Hyper-V reports and asks from a list when there
  are more than one — no name is compiled in.
- **Host capabilities are detected, not configured.** `host_status` reports whether the
  hypervisor is present and whether `vmms`/`vmcompute` are running; `debug_status` says whether
  `kd.exe` was found and where; the window falls back to tkinter when the WebView2 runtime is
  absent, instead of refusing to open.
- **The directories create themselves** under `%LOCALAPPDATA%\HyperVConsoleMCP\` — events,
  transfers, ssh, kd, lab packages and evidence — the first time something needs them.
- **One level per VM replaces the flags.** `hyperv-access set <vm> lab` is the whole
  configuration for a VM, and it is re-read when it changes rather than frozen at startup.

A missing capability comes back as a refusal that carries the call which would fix it, so a
client can either ask you or, in the lab profile, do it.

## Tool groups

Every tool belongs to one toolset. A profile decides which toolsets are registered, and a
tool that is not registered is never advertised, so its schema never enters the model's
context. The `safe` profile registers 30 tools, `lab` registers 45, `HYPERV_MCP_TOOLSETS`
replaces the set outright, and `all` enables everything (67 tools).

| Toolset | Tools | safe | lab |
|---|---|:--:|:--:|
| `core` | `hyperv_host_status`, `vm_list`, `vm_get`, `vm_power`, `checkpoint_list`, `checkpoint`, `events_snapshot`, `operation_status` | ✓ | ✓ |
| `guest` | `guest_report`, `guest_exec`, `guest_transport`, `guest_read_file`, `guest_write_file`, `guest_put_file`, `guest_get_file` | ✓ | ✓ |
| `console` | `console_info`, `console_input`, `console_wait_for_change`, `console_screenshot` | ✓ | ✓ |
| `config` | `guest_autologon_configure`, `guest_config_status`, `guest_config_revert` | ✓ | ✓ |
| `credentials` | `guest_credential_status`, `guest_credential` | ✓ | |
| `media` | `media_list`, `media_mount`, `media_eject` | ✓ | |
| `ssh` | `guest_ssh_provision`, `guest_ssh_status`, `guest_ssh_deprovision` | ✓ | |
| `files` | `file_roots`, `host_list_dir`, `host_make_dir`, `host_rename`, `host_delete`, `guest_list_dir`, `guest_make_dir`, `guest_rename`, `guest_delete` | | |
| `serial` | `serial_status`, `serial_connect`, `serial_read`, `serial_send`, `serial_disconnect` | | |
| `lab` | `lab_init`, `lab_diagnostics`, `lab_stage`, `lab_verify`, `lab_collect`, `lab_guest_configure`, `lab_verifier`, `lab_driver_service`, `lab_run`, `lab_cycle`, `lab_matrix`, `lab_receipts` | | ✓ |
| `kd` | `kd_attach`, `kd_status`, `kd_detach`, `kd_execution`, `kd_command`, `kd_wait_event`, `kd_breakpoint`, `kd_inspect`, `vm_inject_nmi` | | ✓ |
| `debug` | `debug_status`, `debug_setup` | | ✓ |

Families that only differ by an action share one tool with a typed discriminator, so the
schema carries the choice instead of a dozen near-duplicates: `console_input` takes an
ordered list of `text`, `keys`, `key_state`, `mouse_buttons`, `move`, `click`, `scroll`,
`wait` and `ctrl_alt_delete` actions; `checkpoint` takes `create`/`restore`/`delete`;
`guest_report` takes `status`/`processes`/`services`/`events`/`transport`; `guest_credential` takes
`rotate`/`forget`; `lab_receipts` takes `list`/`get`/`compare`/`export`; `kd_execution` takes
`break`/`go`; `kd_breakpoint` takes
`set`/`clear`/`on_load`; `kd_inspect` takes `stack`/`modules`/`symbols`/`analyze`/`dump`
(`symbols` takes the `module` it is about, the same question the window's per-driver badge
asks); and
`debug_setup` crosses `transport` (`serial`/`kdnet`) with `action` (`prepare`/`guest`/
`deprovision`). `vm_get(wait_for=[...])` absorbs the old `vm_wait`. Read-only tools stay
separate from mutating ones, so a listing never asks for approval.

Power actions are `start`, `shutdown`, `save`, `pause`, `resume`, `turn_off`, and `reset`. Forced power needs `confirm_vm` equal to `vm_name`. Restore/delete need the VM name and exact checkpoint GUID confirmed. Overwriting an existing guest file needs `overwrite=true` and an exact `confirm_path`.

`guest_read_file`/`guest_write_file` move small content inline (≤ 8 MiB of base64 in one request). `guest_put_file`/`guest_get_file` copy whole files between the guest and a host path under one of the configured roots (`HYPERV_MCP_TRANSFER_ROOT`, `HYPERV_MCP_LAB_ROOT`, `HYPERV_MCP_EVIDENCE_ROOT`, `HYPERV_MCP_ISO_ROOT`), verifying SHA-256 on both sides; they are the right choice for drivers, dumps and anything else measured in tens of MiB, and they publish `progress` events as the bytes move. Every guest result carries `transport` (`psdirect` or `ssh`); see [client configuration](docs/SETUP.md) for the optional SSH transport, which stays unused until `guest_ssh_provision` has set it up. `guest_report(what="transport")` says which transport a VM is on and, with `probe=true`, times one trivial command over the path the next call would use; `guest_transport` saves a per-VM choice (`auto`/`psdirect`/`ssh`) that applies to the next call, and a `HYPERV_MCP_GUEST_TRANSPORT_<VM>` variable still wins over it.

The `files` family is the browsing half of the same job: `file_roots` names the four host roots and whether each exists, `host_list_dir`/`host_make_dir`/`host_rename`/`host_delete` work inside one of them (a path is resolved and refused if it lands outside its root, so `..` and an absolute path both fail), and `guest_list_dir`/`guest_make_dir`/`guest_rename`/`guest_delete` do the same inside the VM over the configured transport. Deleting anything needs the path spelled back in `confirm_path`; a non-empty directory needs `recursive=true` as well. The dashboard draws this as two panels; the tools exist so an agent can do it too.

The guest password lives in the current user's Windows Credential Manager under `HyperVConsoleMCP/<vm>`, so it never has to sit in the MCP host environment. `guest_autologon_configure` then makes the VM boot straight to the desktop: the password goes into LSA private data rather than the registry, and the no-lock-screen, no-Ctrl+Alt+Del, power, UAC and update-reboot toggles each record the value they replaced in the guest's `C:\DriverLab\State\guest-config.json`, which `guest_config_revert` restores. `guest_credential_status` reports which source is in use and whether PowerShell Direct and SSH accept it without returning the secret; `guest_credential(action="rotate")` changes the password in the guest, proves a fresh session accepts it and only then replaces the stored one. The `HYPERV_MCP_GUEST_USERNAME`/`HYPERV_MCP_GUEST_PASSWORD` pair still works as a fallback.

Media operations use an existing DVD drive and require the VM to be Off. `media_mount(..., boot_first=true)` changes firmware/BIOS boot priority; it does not start the VM. Serial attachment requires an already configured COM1 named pipe and a Running VM. It is not a WinDbg replacement and cannot be combined with a debugger that already owns that pipe.

## Dashboard and reproducible lab

Start the native dashboard with `python -I scripts\run_dashboard.py` or, after installation, `hyperv-dashboard`. `--vm <name>` picks the VM up front; without it the window opens the fleet and asks from a list when there is more than one, so no VM name is built into the program. It uses the same service and policies as MCP and exposes a fleet page, a multi-VM matrix page, workspace tabs and the activity, console, checkpoint, recovery, media, serial/debugging, guest, lab and file-manager pages. It does not rebuild Hyper-V Manager: the fleet's **Hyper-V Manager** button and the console's **Open VMConnect** button start Windows' own tools, and a file can be dragged from one panel of the file manager to the other. Optional per-request approval of AI mutations is documented in [the dashboard guide](docs/DASHBOARD.md); a pending request is a card in a side queue, never a modal prompt, so the window stays usable while an operation waits.

`hyperv-console-mcp` on its own is the same window, and that is the command the Start Menu shortcut runs: a terminal gets the window, an MCP client — which always gives the process a pipe — gets the server, and a shortcut started with no stdin gets the window too. `--stdio` and `--ui` say which one you meant; `--vm NAME` and `--classic` are handed to the window unchanged.

`hyperv-tray` (`pip install hyperv-console-mcp[tray]`) puts the window in the notification area: **Open** starts it, **Status** reports the host, **Quit** closes the icon. Its tooltip carries the live line — `3 VMs · 2 running · lab profile`, or why the service is not answering — so a host that stopped answering is visible without opening anything. It is a client of the same service, keeps no state of its own, and names no VM on the tooltip.

The window is a WebView2 page (`pip install hyperv-console-mcp[ui]`, or pywebview already present). It is loaded from a single HTML file with every asset inline, so it starts no local HTTP server and opens no port. It has a light and a dark theme — following the system until the operator switches it, with the choice kept in the window's own storage and never sent to the service — and every control is reachable by keyboard, including the workspace tabs, the console and the receipt rows. On a host without the WebView2 runtime or without pywebview, `hyperv-dashboard` opens the tkinter window instead of failing; `--classic` asks for that window on any host.

A host where no VM has a level yet opens on a three-question wizard — which VM, what the AI may do with it, and the guest password once — instead of sending a first-time operator to a config file. It writes the same access file the Overview page writes and stores the password through the service, so it grants nothing extra; it stops opening by itself as soon as one VM has a level, and **Set up another VM** on the fleet brings it back. See [the dashboard guide](docs/DASHBOARD.md#the-first-run).

The MCP server and the dashboard are clients of one background service, so a KD or serial session the AI opens is the session the window shows, and the activity page lists what the other process did. A client that finds no host starts one (`hyperv-service`, or `hyperv-service --pipe NAME`) and joins it; `HYPERV_MCP_SHARED_SERVICE=false` keeps a private copy for a run that must not disturb a live one, and `HYPERV_MCP_SERVICE_PIPE` names a different pipe. The host reads its policy once at startup, so whichever client starts it decides the environment for both — set the level per VM in the access file rather than relying on a client's environment.

The lab workflow initializes fixed guest directories, stages hash-verified packages and collects evidence. Package sources and evidence downloads are confined to `HYPERV_MCP_LAB_ROOT` and `HYPERV_MCP_EVIDENCE_ROOT`. Running a package is refused outside `HYPERV_MCP_PROFILE=lab`; in that profile `lab_run`/`lab_cycle` run it, checkpoint first, and classify the outcome as `ok`, `failed`, `not_verified`, `bugcheck`, `hang` or `timeout`. A bugcheck is then triaged from the dump the run collected — the newest minidump first, `!analyze -v` through kd, merged with the host's own crash record — and each cycle writes `lab-cycle.json` beside the evidence so two runs of the same package can be diffed. `lab_receipts` reads those records back on the host alone — `action="list"` names every run newest first, `action="get"` returns one run with the host path and SHA-256 of each evidence file, `action="compare"` reports field by field what changed between two runs, marking a field one side never had as absent rather than as changed to nothing, and `action="export"` copies every receipt into one JSON document under `exports/` without moving the originals. `lab_cycle(record=true)` keeps a GIF of the guest console as evidence; `lab_matrix` runs the same package on two to four VMs, one after another with a run id per VM, and returns the rows plus a field-by-field comparison so the difference is a column rather than a memory. `lab_verifier` turns Driver Verifier on for named test drivers and `lab_driver_service` installs, starts, stops and removes a test kernel-driver service, both recorded and revertible in the guest; a refused driver is reported with its Win32 error and NTSTATUS, and signature enforcement is never bypassed. See [the lab guide](docs/LAB.md); no driver or mapper is bundled or run by this repository.

## Performance and debugging

The server reuses one local PowerShell worker for host and console work and a second one for guest operations, so a long `guest_exec` does not stall `vm_get` or console input. Inside each worker it keeps one PowerShell Direct session per VM, so a guest request does not pay for a new remoting connection. An upload uses `Copy-VMFile` when the guest's Guest Service Interface is enabled and falls back to `Copy-Item -ToSession` otherwise; downloads use `Copy-Item -FromSession`, since `Copy-VMFile` only goes host to guest. Every copy is SHA-256 verified on the far side. The fake-worker benchmark measures startup and warm requests without Hyper-V; it is not presented as a live VM benchmark. PowerShell Direct remains the default for isolated guests. Optional SSH is appropriate only for frequent guest administration on an intentionally configured isolated network.

For kernel work, choose **KDNET** after reviewing a suitable network path, or **COM1/named pipe** to preserve a no-network VM. Guest BCD and host COM1 preparation are separate, explicitly confirmed operations. Neither stops nor reboots the VM automatically.

`kd_attach` then drives a real `kd.exe` from the server: one session per VM, a per-session marker that delimits every command's output (never a sleep), an explicit `disconnected → waiting_for_target → running ↔ broken` state reported by `kd_status`, break-in through the console with `vm_inject_nmi` as the fallback, and a full log under `%LOCALAPPDATA%\HyperVConsoleMCP\kd\<vm>\`. `debug_setup(transport="kdnet", action="prepare")` sets up the internal lab switch, a UDP port in 50000–50039 and a host firewall rule that names `kd.exe`; the generated key lives only in the Windows Credential Manager and is masked in everything the tool returns. Symbols come from the Microsoft symbol server into a local cache, plus any staged lab package that ships a `.pdb`. In the lab profile, `lab_cycle(..., debugger="kdnet")` attaches kd before the run, arms `sxe ld:<driver>` for each `.sys` in the package manifest, and reports a bugcheck with the parsed `!analyze -v` instead of calling it a hang, together with whether the test driver's symbols actually resolved.

- [Start here: install and first run, in plain language](docs/QUICKSTART.md)
- [Performance rationale and benchmarks](docs/PERFORMANCE.md)
- [Driver testing: the QA flow](docs/DRIVER-TESTING.md)
- [WinDbg, KDNET, and serial setup](docs/WINDBG.md)
- [Optional SSH transport](docs/SSH.md)
- [Guest access: autologon, no prompts, revert](docs/GUEST-ACCESS.md)

## Security boundaries

- Local stdio only; no HTTP server or unauthenticated network listener.
- Read-only mode by default; allowlist required for every mutation.
- The approval gate decides whether a tool call may proceed; it is not a sandbox. It does not protect against an agent that already has a shell on the host, and the lab profile deliberately gives an autonomous loop full control of the allowlisted VMs.
- `HYPERV_MCP_PROFILE=lab` enables every capability for the allowlisted VMs and skips the per-mutation dashboard approval. It does not widen the allowlist, skip a `confirm_*` field, or stop the audit and event records; an explicit `HYPERV_MCP_ALLOW_*=false` still wins. `lab_run`/`lab_cycle`, which execute a staged package, exist only in this profile.
- Console input is real input to the VM and can change state immediately.
- The `kd_*` tools follow the same allowlist and require `HYPERV_MCP_ALLOW_DEBUGGER`; the KDNET key is generated on the host, stored in the Windows Credential Manager and masked in every result. The host-side KDNET change is one firewall rule for `kd.exe` on one UDP port of the internal lab switch — never an external switch.
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

CI runs the suite on Windows 3.11/3.12/3.13 with ruff, PowerShell parsing and PSScriptAnalyzer, the MCP smoke and the build, and runs the unit suite on Linux, where the Windows-only tests skip themselves. `tests/test_secrets.py` also scans a deliberately leaking build, so a scan that cannot turn red fails the run instead of passing quietly.

The PowerShell bridge accepts fixed operations and JSON over stdin, not generated PowerShell source. The fake bridge allows transport tests without Hyper-V. Live testing must be separately authorized and restricted to a disposable VM.

For an elevated, read-only end-to-end diagnostic through a real MCP stdio client:

```powershell
python -I tests\live_mcp_readonly.py --vm NORTH-Test
```

This queries host, VM, console, COM/debugger status, media, and checkpoints. It does not change VM or guest state.

Live checks that do mutate a VM exist as separate scripts and never run in the pytest suite. Each reads `HYPERV_MCP_LIVE_TEST_VM` (or `--vm`) and expects a disposable guest:

| Script | What it proves |
|---|---|
| `tests/live_readonly.py` | host, VM, console and debugger status, no changes |
| `tests/live_mcp_readonly.py` | the same through a real MCP stdio client |
| `tests/live_service_host.py` | a real host in another process, an MCP client over its pipe, and a second client reading the events the first produced |
| `tests/live_ssh.py` | SSH provisioning, transport round trip, deprovision |
| `tests/live_lab_publish.py` | atomic package staging, host-side verification and the publish refusal |
| `tests/live_guest_automation.py` | autologon + toggles, thumbnail screenshot, revert |
| `tests/live_kd.py` | KDNET or serial attach → break → stack → detach |
| `tests/live_cycle.py` | `lab_cycle` end to end with a harmless package |
| `tests/live_large_payload.py` | a ~100 KB `guest_exec` and file transfer over both transports |
| `tests/live_guest_session.py` | the session is reused, `Copy-Item` carries a file both ways, and `--reboot` proves a reboot opens a new session |
| `tests/live_files.py` | the file manager: a folder on each side, a file copied both ways with matching digests, the progress events, and a delete refused without its confirmation |

## License

MIT — see [LICENSE](LICENSE).
