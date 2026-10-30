# Project journal — hyperv-console-mcp

## Purpose

An independent, local Model Context Protocol server for Hyper-V on Windows. It combines VM inventory, lifecycle, checkpoints, PowerShell Direct, synthetic keyboard/mouse input, and experimental VMConnect capture. This repository does not contain or execute NORTH, a driver, a mapper, or third-party Hyper-V MCP code.

## Design contract

- Python 3.11+ and MCP Python SDK 2.x (`MCPServer`, not the removed `FastMCP` module).
- Local stdio transport only. No network listener or remote authentication surface.
- A replaceable `HyperVBackend` protocol keeps policy tests independent of a live host.
- One versioned PowerShell bridge is shipped as package data. Requests use JSON on stdin, never interpolated PowerShell source.
- Read-only operations work without guest credentials. Mutations require capability flags and a VM allowlist.
- Forced power actions, checkpoint restore/deletion, and guest file overwrite require exact confirmation fields.
- Guest passwords come from environment variables, cross stdin to the local bridge, and are never placed in tool arguments or audit records.
- Keyboard text, guest commands, file contents, credentials, and command output are not written to the audit log.
- Mouse coordinates are guest-display pixels. Console screenshots are experimental because VMConnect belongs to the interactive Windows session and may scale its display.

## Initial milestones

1. Project skeleton, MCP 2.3 API verification, policy and fake-backend tests.
2. Read-only inventory, lifecycle, checkpoints, PowerShell Direct, file transfer, and synthetic input.
3. Experimental VMConnect screenshot and a real MCP client smoke test.
4. Opt-in validation against the disposable `NORTH-Test` VM, without loading any driver.
5. Documentation, repository review, and publication after GitHub authentication is repaired.

## Changelog

### 2026-10-28 — Initial implementation

- Selected `MCPServer` from `mcp.server.mcpserver` after confirming MCP SDK 2.3.0 is installed.
- Confirmed the host exposes `Msvm_Keyboard.TypeText`, `TypeKey`, `PressKey`, `ReleaseKey`, `TypeCtrlAltDel`, and `Msvm_SyntheticMouse` absolute positioning, button, and scroll methods.
- Confirmed VMConnect is running, while the unelevated shell receives an access-denied error from `Get-VM`.
- Established the independent project and its security-first architecture. No VM mutation or driver execution has been performed.
- Implemented 21 MCP tools: host/VM, power, checkpoints, PowerShell Direct, bounded guest file transfer, console input/capture, and serial debugger status/preparation.
- Replaced per-call PowerShell startup with a persistent, locked JSON-line worker and tests for crash/timeout recovery.
- A fake-worker benchmark measured 0.153 seconds for the cold request and 0.002 seconds for nine warm requests; these figures are explicitly not live Hyper-V benchmarks.
- A real MCP stdio client initialized, discovered 21 tools, and blocked a disabled power mutation. Live host reads returned the expected `access_denied` error in the unelevated shell.
- Direct VMConnect capture is implemented without desktop fallback. Testing correctly rejected a minimized window; no valid live guest screenshot has yet been obtained.
- Added WinDbg serial named-pipe preparation, with the VM required to be Off and no automatic guest reboot. KDNET remains documented but is not automatically configured because it requires a deliberate network design.
- Built the Python wheel successfully. GitHub CLI authentication remains invalid and no repository has been pushed.
- Added Windows CI for Python 3.11/3.12/3.13; the workflow is not claimed as run until GitHub executes it.
- Current local suite: 28 passing unit/transport tests, including worker crash and timeout recovery. A guarded live read-only diagnostic is available at `tests/live_readonly.py` for an already elevated shell.

### 2026-10-28 — QEMU-inspired management expansion

- Expanded the server to 38 tools: pause/resume, state waiting, guest health/process/service/event diagnostics, explicit input states and console batches, ISO/boot management, serial console transport, and separate host/guest debugger preparation.
- Added `HYPERV_MCP_ALLOW_MEDIA`; all media mutations require confirmation and a VM that is already Off. No implicit power or network change was introduced.
- Added a dedicated, cancellable Windows named-pipe reader with bounded retained output and a persistent `.log`. It does not block the main PowerShell worker.
- Split guest BCD preparation (Running VM, PowerShell Direct) from COM1 preparation (Off VM), removing the original impossible attempt to reach a stopped guest through PowerShell Direct.
- Deliberately omitted a clipboard tool until a real guest clipboard mechanism is implemented; synthetic Ctrl+V would not provide clipboard semantics.
- All console batches are schema-checked before the first input action. A batch stops on the first operational failure and is not transactional.
- Local suite: 37 passing tests, including a real local Windows named-pipe read/write/log/disconnect test. PowerShell bridge syntax and the 38-tool MCP stdio smoke test pass.
- Live Hyper-V reads remain blocked by `access_denied` in the unelevated development shell. No live VM mutation, media change, COM change, guest BCD change, debugger attachment, driver, or mapper was executed.
- GitHub publication of the base remains pending the OAuth `workflow` permission. Expansion changes have not been committed or pushed.

### 2026-10-28 — Initial GitHub publication

- Pushed `main` to `https://github.com/maiconzeraxd/hyperv-console-mcp`, including initial commit `704dcf1` and expansion commit `3e24373`.
- GitHub Actions created run `40689509446`, but all three Windows/Python jobs were rejected before starting because the account was locked for a billing issue. No CI test or build step executed; this is not a code test failure.
- Local validation remains 37 passing tests, a successful MCP stdio smoke test, valid PowerShell syntax, and successful sdist/wheel builds.

### 2026-10-28 — Elevated live read-only MCP validation

- A real elevated MCP stdio client successfully queried `hyperv_host_status`, `vm_get`, `console_info`, `debug_serial_status`, `media_list`, and `checkpoint_list` for the running `NORTH-Test` VM. All six calls succeeded; no VM or guest mutation was performed.
- Fixed `debug_serial_status` compatibility with the host's Hyper-V version: COM numbers are parsed from the `COM 1`/`COM 2` port names because `Get-VMComPort` does not expose a `Number` property there.
- Confirmed COM1 debugger mode is On but its pipe path is empty. No COM, BCD, media, checkpoint, network, or power configuration was changed.
- Added a mocked real-bridge regression test and `tests/live_mcp_readonly.py`, which supports an optional JSON report. PowerShell syntax remains valid.
- These changes were subsequently incorporated into the dashboard/lab and hardening delivery below.

### 2026-10-28 — Dashboard, reproducible lab and bridge hardening

- Added a native tkinter dashboard with seven pages, shared policy enforcement, cross-process activity and optional current-user-only approval of AI mutations. Local serial and screenshot actions are covered by the approval gate as well as bridge mutations.
- Added five non-executing lab tools for initialization, diagnostics, package staging/verification and evidence collection, plus an activity-snapshot tool. Packages use bounded manifests and SHA-256 verification; payload execution remains a separate manual guest action.
- Hardened the real PowerShell bridge: warning-safe protocol output, exact VM matching, checkpoint return values, consistent remoting DTOs, native stderr/exit-code preservation, empty event-log handling, Secure Boot preflight before BCD, and structured error classification.
- Serial now uses one overlapped handle for reads and writes, bounded write waits and cancellation before handle closure. Console batches validate every action and their cumulative wait budget before sending input. Keyboard text is explicitly limited to the synthetic keyboard's supported ASCII input.
- Confined ISO and serial-log paths to configurable roots, increased the safe default guest-read limit to 64 KiB, added schema enums, and made the package/MCP version single-sourced.
- Added nested-operation event regression coverage, real-bridge mocked tests, policy tests, dashboard/lab tests, Ruff, PowerShell syntax CI, MCP stdio smoke CI and a non-Windows import job. GitHub execution is not claimed until Actions actually runs.
- Local validation: 71 passing tests, clean Ruff checks, valid PowerShell syntax, successful 44-tool stdio/policy-denial smoke, and successful sdist/wheel builds. The unelevated smoke shell cannot read the live host; the earlier elevated read-only validation remains separate.
- No live mutation, guest BCD change, package execution, driver or mapper run was performed during this delivery.

### 2026-10-29 — v0.2 phase 1: bridge and lab correctness

- Fixed the bridge's `-ArgumentList`/`-Arguments` mismatch that broke `guest_read_file`, `guest_write_file`, `guest_read_chunk` and `guest_assemble_file`; a mocked-real-bridge test now asserts the bound parameter names and argument count for each operation plus the new `guest_publish_package`.
- `guest_exec` now reports `had_errors`/`error_records` from the runspace error collection, so a non-terminating cmdlet error makes `success` false while native stderr from a successful command stays output. Regression tests cover both directions.
- Graceful `shutdown`/`save` run as background Hyper-V jobs and return `{accepted, operation_id}`; the new READ-ONLY `operation_status` tool tracks them. `turn_off`/`reset` remain synchronous. The server now exposes 45 tools.
- `HYPERV_MCP_LAB_ROOT`, `HYPERV_MCP_EVIDENCE_ROOT` and `HYPERV_MCP_EVENT_DIRECTORY` are read into `Settings` at startup and must be absolute.
- Canonical manifest hashing now agrees between Python and Windows PowerShell 5.1: package paths are restricted to `[A-Za-z0-9._-]` per `/`-separated segment on both sides, the module escapes JSON strings by hand and sorts keys with `[StringComparer]::Ordinal`. Tests compare real hashes for special characters and ordinal ordering.
- `Test-DriverLabPackage.ps1` fails on files the manifest does not describe; the run entry point must be a listed `Run-Lab.ps1` manifest member. Staging is all-or-nothing under `C:\DriverLab\Transfers\<transfer-id>\<package-id>` with an in-guest re-verification before a single directory rename publishes it, and `init` prunes transfers older than 24 hours.
- A missing guest file is now `not_found` (the guest reports absence as data and the host raises a typed error, because remoting does not preserve guest exception types); `lab_init` installs tools only on `not_found` instead of overwriting after any bridge error.
- PSScriptAnalyzer 1.25 runs in CI with default rules and reports no errors or warnings; Ruff is pinned to 0.17.3.
- Local validation: 102 passing tests, clean Ruff, valid PowerShell syntax, clean PSScriptAnalyzer.
- Not yet validated live: the publish/atomic-move path and the guest-side verification. `tests/live_lab_publish.py` covers them against a disposable VM and has not been run in this delivery. No VM, guest, driver, mapper or payload was executed.

### 2026-10-29 — v0.2 phase 1: approval and IPC hardening

- The approval broker no longer dies after four approvals. Each served instance now calls `FlushFileBuffers` and `DisconnectNamedPipe` before returning to `ConnectNamedPipe`; a malformed or abandoned request disconnects and continues instead of ending the instance. A Windows-only test drives 20 sequential and 4 concurrent approvals through one broker.
- The first pipe instance is created with `FILE_FLAG_FIRST_PIPE_INSTANCE`, so a second broker fails with a clear error instead of silently sharing the name, and the client verifies the server before sending anything: `GetNamedPipeServerProcessId`, `QueryFullProcessImageNameW` must be a Python image, and the process token SID must equal the current user's.
- The approval gate moved from the bridge to the tool level. `EventSink.invoke` no longer asks per operation, so one `lab_stage` requests one approval instead of one per file and chunk and one `console_batch` requests one instead of one per action. An `approved_scope` contextvar makes nested calls inherit the granted approval; read-only tools (`lab_verify`, `lab_diagnostics`, `guest_status`, `events_snapshot`) never ask. `GATED_TOOLS` lists the 25 mutating tools and a test parses `server.py` to prove every `MUTATION`-annotated tool is listed and gated in `service.py`.
- Approval requests carry a secret-free summary built by the tool: power action, checkpoint name or ID, guest path with size and SHA-256, package ID with file count, byte total and manifest hash, media path and drive, or the first 20 lines of a `guest_exec` command plus the SHA-256 of the full command. Typed console and serial text is reported only by length. The broker scrubs credential-shaped keys and rejects oversized summaries as a backstop, and the dashboard renders the summary in a scrollable modal instead of pointing at the conversation.
- Local validation: 110 passing tests, clean Ruff, valid PowerShell syntax.
- Not yet validated live: the hardened broker has only been exercised by the Windows-only unit test in this session, not by a real MCP server and dashboard pair across processes.

### 2026-10-29 — v0.2 phase 1: event log and cross-platform tests

- Events are now appended to `events-YYYYMMDD.jsonl` (one JSON record per line) under a process lock instead of one file per event. `HYPERV_MCP_EVENT_RETENTION_DAYS` (default 14) prunes older daily files on the first write of a process, and `EventLogReader` tails the logs by byte offset so the dashboard's 1.5 s poll no longer re-reads and re-sorts the whole history.
- Windows-only tests are marked: real named-pipe approval in `tests/test_events.py` and the drive-letter media tests in `tests/test_service.py`; `tests/test_dashboard.py` uses `pytest.importorskip("tkinter")`.
- Local validation: 113 passing tests, clean Ruff.
- Not yet validated: the JSONL log and the offset reader have not been exercised by two processes writing at once outside the unit tests.

### 2026-10-29 — v0.2 phase 2: guest transport abstraction

- Guest access now goes through `src/hyperv_console_mcp/transport/`: a `GuestTransport` protocol (`exec`, `put_file`, `get_file`, `health`, `call`, `close`) with `PowerShellDirectTransport` as the only implementation, and `select_transport` choosing per VM from `HYPERV_MCP_GUEST_TRANSPORT` (`auto`/`psdirect`/`ssh`) or a `HYPERV_MCP_GUEST_TRANSPORT_<VMNAME>` override. `auto` and `psdirect` both resolve to PowerShell Direct; an explicit `ssh` raises a policy error rather than falling back silently, because SSH provisioning does not exist yet.
- Every guest tool result now names its transport. `guest_exec` returns the mapped `ExecResult` (`success`, `exit_code`, `output`, `had_errors`, `error_records`, `output_truncated`, `transport`).
- New `guest_put_file`/`guest_get_file` copy whole files between the guest and a host path under `HYPERV_MCP_TRANSFER_ROOT` (`HYPERV_MCP_TRANSFER_ROOT`, default `%LOCALAPPDATA%\HyperVConsoleMCP\transfers`), verifying SHA-256 on both sides. `guest_get_file` downloads in 4 MiB chunks into a `.part` file, checks each chunk, then renames; a corrupt chunk leaves nothing behind. Both are gated and their approval summaries name the paths, size and digest.
- Added the `guest_file_info` bridge operation (size and last-write time, or `not_found` reported as data) so a download can size itself before reading.
- Local validation: 128 passing tests, clean Ruff, valid PowerShell syntax.
- Not yet validated live: the transport has only been exercised against a fake bridge. The persistent `New-PSSession -VMName` session, `Copy-Item -ToSession`, reconnect-on-reboot and the second worker are **not implemented yet** — the transport still calls the existing per-request `Invoke-Command` path, so `put_file` is bounded by the 8 MiB inline limit and `get_file` reads in 4 MiB chunks. SSH, `guest_ssh_provision` and the transport benchmark are likewise still pending.

### 2026-10-29 — v0.2 phase 2: one persistent guest session and Copy-Item transfer

- The bridge keeps one `New-PSSession -VMName` per VM in `$script:GuestSessions` and reuses it for every guest request, instead of opening a remoting connection per operation. A session whose `State` is not `Opened` is dropped, and so is one whose VM uptime went **down**, which is how a guest reboot is detected without an extra remote call. Sessions are closed when the worker's stdin loop ends.
- If a runspace dies between requests the failure is not cached: the session is removed and the next request opens a fresh one. The request that hit the dead session still fails rather than being retried, because a retry could re-run a command that already had side effects.
- File transfer moved off base64-in-JSON: `guest_put_file` and `guest_get_file` now use `Copy-Item -ToSession`/`-FromSession` over that session, hash the file on both sides and refuse to publish a copy whose SHA-256 disagrees. Downloads land in a `.part` file next to the target and are renamed only after the digest matches, so an interrupted transfer never leaves a truncated file under the caller's name. Uploads create the guest parent directory and refuse to replace an existing file unless `overwrite` is set.
- Local validation: 132 passing tests, clean Ruff, valid PowerShell syntax, PSScriptAnalyzer clean on `src` (the analyzer rejected an inline scriptblock passed to `Invoke-Command` and two empty catch blocks, so the guest scriptblocks are now named script-scope values and the catches log at verbose level).
- Not yet validated live: no request has been served from a real `New-PSSession`. The mocks model the session, `Copy-Item -ToSession` and the reboot path; only a disposable VM can prove PowerShell Direct reuses a session and that `Copy-Item` behaves as expected across a real reboot. The second worker for guest operations, `scripts/bench_transport.py` and `docs/PERFORMANCE.md` are still pending.

### 2026-10-29 — v0.2 phase 2: transport benchmark

- `scripts/bench_transport.py` measures the configured transport against a disposable VM: ten small guest commands, then an upload and a download for each requested size (1 MiB and 100 MiB by default). The JSON result carries host, platform, VM, date and both digests, because a number without them is not a measurement. `docs/PERFORMANCE.md` records the reasoning and the command.
- Not yet validated live: the script has not been run against a VM in this delivery, so `docs/PERFORMANCE.md` deliberately contains no live figures.

### 2026-10-29 — v0.2 phase 2: SSH transport client

- Added `SshTransport` in `src/hyperv_console_mcp/transport/ssh.py`. It keeps one paramiko `SSHClient` per VM (keyed by host, port, user and key path), sets a 30 s keepalive, and reconnects when the connection is no longer active. paramiko rather than asyncssh because the service layer is synchronous and already runs each call off the event loop; an asyncio client would need a loop per worker thread for no benefit.
- Host keys are verified strictly against a tool-owned `known_hosts` in `%LOCALAPPDATA%\HyperVConsoleMCP\ssh\<vm>\` with `RejectPolicy`; there is no trust-on-first-use path and `look_for_keys`/`allow_agent` are off. The key is `id_ed25519` in the same directory. `HYPERV_MCP_SSH_ROOT` moves that root.
- `exec` wraps the command in the same semantics as the bridge (`$Error` delta for non-terminating cmdlet errors, native stderr kept as output) and parses a base64 JSON marker from stdout. Transfers use SFTP with a `.part` file, and both directions compare SHA-256 against the guest's own `Get-FileHash`; a mismatched upload is deleted from the guest and a mismatched download never reaches the target name.
- Structured guest operations (processes, services, events, file metadata, read/write/chunk/assemble/publish) run `C:\ProgramData\HyperVConsoleMCP\guest_operations.ps1`, shipped as `transport/guest_operations.ps1`, so both transports execute the same PowerShell and return the same JSON.
- `select_transport` now returns SSH for an explicit `ssh` choice only when the VM is provisioned (otherwise a policy error naming `guest_ssh_provision`), and `auto` uses SSH only when it is provisioned and answers a health probe within 2 s; the probe result is cached for 30 s so `auto` does not pay a connection round trip per tool call.
- paramiko is an optional extra (`pip install 'hyperv-console-mcp[ssh]'`) imported lazily, so `auto` and `psdirect` users never need it. Without it an SSH selection fails with that install hint.
- Local validation: 153 passing tests (21 new in `tests/test_ssh_transport.py` against a stub paramiko), clean Ruff, valid PowerShell syntax, PSScriptAnalyzer reports no errors or warnings on `src`.
- Not yet validated live: nothing here has touched a real sshd. `guest_ssh_provision`, `guest_ssh_status` and `guest_ssh_deprovision` are **not implemented yet**, so no state file exists to select this transport; the guest script has never run on Windows, and the PowerShell 5.1 single-item array collapse is compensated on the Python side but unproven against a real `Get-WinEvent` result.

### 2026-10-29 — v0.2 phase 2: guest SSH provisioning

- `guest_ssh_provision(vm_name, confirm_vm, use_existing_adapter=false)` now builds the whole SSH path over PowerShell Direct, because SSH cannot exist in the guest until this runs. Order: guest health first (a dead guest fails before the host is touched), then the host network, then the key, then the guest script, then the pinned host key and the state file, then a health probe over the new transport. If the probe fails the state file is deleted, so `auto` cannot prefer a transport that never answered.
- Host side (`bridge/HyperVBridge.ps1`): `ssh_network_prepare` creates an **internal** `HyperV-MCP-Lab` switch and a `HyperV-MCP-Lab` adapter on the VM, and puts `10.77.0.1/<prefix>` on `vEthernet (HyperV-MCP-Lab)`. The adapter decision happens before anything is created, so a refused request (the VM already has a NIC and `use_existing_adapter` was not passed) leaves no switch behind; an existing switch of another type is refused rather than reused. `ssh_network_remove` undoes exactly what the state file says was created and never deletes the switch. `Convert-Vm` now reports each adapter's MAC address, which is how the guest finds its own lab NIC.
- Guest side (`transport/guest_ssh_setup.ps1`, `-Mode Install|Status|Remove`): installs sshd from the `OpenSSH.Server` capability and falls back to an extracted Win32-OpenSSH package (`HYPERV_MCP_OPENSSH_PACKAGE`) when the guest has no internet; writes a managed `PubkeyAuthentication yes` / `PasswordAuthentication no` block above any `Match` block after backing up the original; sets `HKLM:\SOFTWARE\OpenSSH\DefaultShell` to `powershell.exe` so both transports run the same commands; installs one key in `administrators_authorized_keys` with an ACL of Administrators and SYSTEM only; adds a TCP 22 rule scoped to the host address; and gives the lab NIC the stable guest address.
- Host key pinning: `install` returns `ssh_host_ed25519_key.pub` and it is written to `%LOCALAPPDATA%\HyperVConsoleMCP\ssh\<vm>\known_hosts`. There is no trust-on-first-use path anywhere.
- `guest_ssh_status` is read-only and reports the guest-side state plus a live SSH health probe. `guest_ssh_deprovision` removes the firewall rule, the managed key, the guest address, the pinned key and state file, and detaches (or deletes, when provisioning created it) the lab adapter.
- Addresses: the host takes the first usable address of `HYPERV_MCP_SSH_SUBNET` and each VM gets a stable offset derived from `sha256(vm_name)`, so a re-provision does not move the guest and two VMs do not collide. `_subnet` rejects non-IPv4 and prefixes above /30.
- Bug found while testing: `_restrict_acl` originally ran `icacls /inheritance:r /grant:r <user>:F` on the key directory, which removes inherited ACEs from files already inside it and leaves them unreadable by their own owner — the key directory locked itself. It now grants `(OI)(CI)F` recursively first and only then cuts inheritance.
- Local validation: 168 passing tests (8 new in `tests/test_ssh_setup.py` for address derivation, keypair reuse, the provisioning sequence, `known_hosts` and `state.json` contents, the offline-package upload, and the two failure paths; 7 new bridge tests for `ssh_network_prepare`/`ssh_network_remove`), clean Ruff, all PowerShell files parse, PSScriptAnalyzer reports no errors or warnings on `src` (the new `Set-*` helpers declare `SupportsShouldProcess` and read their values through script-scoped names).
- Not yet validated live: no provisioning has run against a real guest. The capability install, `install-sshd.ps1`, the sshd host-key timing loop, `Set-NetIPAddress` on the lab NIC and the firewall rule are all modelled only by mocks. `guest_ssh_status`/`guest_ssh_deprovision` likewise. The second worker for guest operations is still pending, so a long `guest_exec` still blocks `vm_get` and the console.

### 2026-10-29 — v0.2 phase 2: guest operations get their own worker

- `GuestSplitBackend` in `backend.py` runs two PowerShell workers: every bridge operation named `guest_*` goes to a guest worker, everything else (VM state, console, media, serial, the host-side `ssh_network_*` operations) stays on the host worker. A long `guest_exec` therefore no longer blocks `vm_get` or console input, which was the remaining stall in the roadmap.
- Both workers start lazily, so a server that never touches a guest still runs one process. Each worker keeps its own `New-PSSession -VMName` per VM, which is what makes the split work; the cost is one extra session per VM once guest tools are used.
- The MCP server and the dashboard now construct `GuestSplitBackend`, and `select_transport` still receives the same backend object, so PowerShell Direct guest traffic is routed to the guest worker without any transport change.
- Local validation: 171 passing tests (3 new: guest/host routing, a blocked guest call that does not delay a host call, and close closing both), clean Ruff.
- Not yet validated live: the split has only been exercised against recording fakes. Two real workers holding a PowerShell Direct session to the same VM, and the bridge's per-process session dictionary on each of them, still need a disposable VM.

### 2026-10-29 — v0.2 phase 2: live SSH check script

- `tests/live_ssh.py` provisions SSH on a disposable VM named by `HYPERV_MCP_LIVE_TEST_VM`, runs `guest_exec` and `guest_status` over the new transport, prints the status before and after, and can deprovision at the end. It refuses to run without a VM name, reads the guest password only from the environment, and is not collected by pytest.
- Not yet validated live: the script has not been run. It is the checklist for the first disposable VM, not evidence that provisioning works.

### 2026-10-29 — v0.2 phase 3: guest credential in the Windows Credential Manager

- The guest password no longer has to live in the MCP host environment. `credentials.py` wraps `CredReadW`/`CredWriteW`/`CredDeleteW` through ctypes and stores one generic credential per VM under `HyperVConsoleMCP/<vm>`, persisted for the current user (`CRED_TYPE_GENERIC`, `CRED_PERSIST_LOCAL_MACHINE`). The blob is the UTF-16-LE password, which is what the Windows Credential Manager UI expects for a generic credential. The module refuses to import its Windows entry points off Windows, so the Linux CI path raises a policy error instead of an `AttributeError`.
- `credentials.resolve(settings, vm)` reads the vault first and falls back to `HYPERV_MCP_GUEST_USERNAME`/`HYPERV_MCP_GUEST_PASSWORD` only when no stored password exists; a stored entry always wins. `credentials.source()` reports `credential_manager`, `environment` or `missing`, so the weaker source stays visible instead of silently working. Both transports now resolve credentials through it: `PowerShellDirectTransport._credentials` and `SshTransport._username`.
- New MCP tools: `guest_credential_status` (read-only: source, account, whether PowerShell Direct and SSH accept the credential, never the secret), `guest_password_rotate` and `guest_credential_forget`. `guest_credential_set` exists only on the service and the dashboard — there is no MCP tool that takes a password, so the AI cannot type one.
- Rotation order is deliberate: generate a random 24-character password, set it in the guest (`guest_set_password`), prove a **fresh** `New-PSSession -VMName` accepts it (`guest_auth_check`), and only then replace the vault entry. A guest that rejects the new password gets the previous one written back and the call fails with `password_rotation_failed`, so the stored credential is never the one that stopped working.
- Two new bridge operations: `guest_set_password` and `guest_auth_check`. The first builds its `SecureString` character by character instead of using `ConvertTo-SecureString -AsPlainText`, which PSScriptAnalyzer rejects as an error; the secret travels as a base64 JSON field, never as part of a command line. The second opens a brand-new session with the supplied credential rather than reusing the worker's cached one, because a cached session proves nothing about the new password.
- Dashboard: a **Guest access** page with the current source, an explicit refresh, and buttons to set the credential (masked modal dialog), rotate it, test PowerShell Direct and SSH, and forget it. The page deliberately does not query the guest on load — reading the status needs the guest capability, and a disabled capability should not raise a dialog just because the page was opened.
- Local validation: 186 passing tests (9 new in `tests/test_credentials.py`, 3 in `tests/test_service.py`, 3 bridge regressions for the password change and the auth check), clean Ruff, all PowerShell files parse, PSScriptAnalyzer reports no errors or warnings on `src`.
- Not yet validated live: no credential has been written to, or read from, a real Credential Manager, and no password has been rotated in a real guest. `Set-LocalUser` on a domain-joined account, the LSA round trip and the fresh-session proof are modelled only by mocks and by a Windows-only vault round-trip test with a throwaway target. Autologon, the no-lock-screen and UAC-prompt settings, `guest-config.json` and `guest_config_revert` are still pending.

### 2026-10-29 — v0.2 phase 3: autologon and the automation toggles

- `guest_autologon_configure(vm_name, enabled, confirm_vm, restart=false, lock_screen, disable_cad, power, uac, windows_update)` boots the guest straight to the desktop. `enabled` and every toggle are tri-state: `true` applies the setting, `false` restores the value it replaced, and omitting it leaves that setting alone, so a caller can change one toggle without stating the others. The MCP schema keeps `enabled` required; the dashboard sends `None` when the operator only wants a toggle.
- The autologon password goes into the LSA private data key `DefaultPassword` through `LsaStorePrivateData`, the route Sysinternals Autologon uses, so it is not in the registry. Applying also sets `AutoAdminLogon`, `DefaultUserName`, `DefaultDomainName`, removes any residual `DefaultPassword` registry value, removes `AutoLogonCount` (a non-zero count logs on once and then stops), and sets `DevicePasswordLessBuildVersion=0`, without which Windows 10/11 ignore autologon. A blank password is refused rather than offered, because Windows restricts passwordless accounts to console logon and that breaks PowerShell Direct and SSH.
- Toggles: `NoLockScreen` under the Personalization policy key; `DisableCAD` under the system policy key; `powercfg` standby, monitor and `SUB_NONE CONSOLELOCK` set to 0 plus the screensaver off; `ConsentPromptBehaviorAdmin=0` with `EnableLUA` left at 1 (the tool refuses to run if LUA is 0 rather than turning UAC off); and `NoAutoUpdate`/`NoAutoRebootWithLoggedOnUsers` for the optional update toggle.
- Every change is recorded in `C:\DriverLab\State\guest-config.json` with the value it replaced (registry kind and data, whether the LSA secret existed, the previous `powercfg` AC index). A second Apply keeps the first record, so revert always returns to the state before this tool touched anything. `guest_config_revert` restores all of it and deletes the state file; `guest_config_status` is read-only and reports the live values, the managed entries and any interactive session.
- The settings document (the only thing that carries the password) is written to a per-call file under `C:\ProgramData\HyperVConsoleMCP`, which the guest script deletes before it changes anything; on a failure the host tries once more to remove it. The password never appears on a command line.
- `restart=true` is the only path that touches power: graceful `shutdown`, wait for Off, `start`, wait for Running, then `query user` until an interactive session appears. It requires `HYPERV_MCP_ALLOW_POWER` and fails with `restart_timeout` instead of escalating to a forced power-off.
- Dashboard: the Guest access page gained one `leave`/`on`/`off` row per setting, an optional restart, and Apply / Read automation state / Revert buttons.
- Local validation: 203 passing tests (8 new in `tests/test_guest_config.py`, 11 in `tests/test_service.py`), clean Ruff, all PowerShell files parse, PSScriptAnalyzer reports no errors or warnings on `src`.
- Not yet validated live: nothing in this delivery has run against a Windows guest. The `Add-Type` compilation of the LSA P/Invoke, `LsaStorePrivateData` on a real LSA policy, the registry writes, the `powercfg` indices, `query user` parsing after a real autologon, and whether Win10/11 actually honour the LSA secret in the target VM are all modelled only by mocks. `guest_config_status` uploads its helper script on every call, so a strictly read-only guest policy would need that upload allowed.

### 2026-10-29 — v0.2 phase 4: the lab profile

- `HYPERV_MCP_PROFILE=safe|lab` (`safe` by default). `lab` turns the seven `HYPERV_MCP_ALLOW_*` capabilities on for the allowlisted VMs and makes `_approval_required()` return false, so the autonomous loop does not wait for a dashboard click on every step. An explicit `HYPERV_MCP_ALLOW_<CAPABILITY>=false` still wins, so one capability can be held back inside the profile.
- What the profile deliberately does not touch: the allowlist (an empty `HYPERV_MCP_ALLOWED_VMS` still refuses every mutation, and `_write` checks that before anything else), the `confirm_*` fields, the confined host paths, the ban on host driver/payload execution, and the audit and event records, which still record every operation.
- `hyperv_host_status` now also reports `profile`, `capabilities` (the seven flags) and the allowlist, and the dashboard sidebar shows the active profile next to the VM name.
- Local validation: 212 passing tests (5 new in `tests/test_config.py`, 3 in `tests/test_service.py`), clean Ruff.
- Not yet validated live: no server has been started with `HYPERV_MCP_PROFILE=lab` against a real VM. The profile changes only configuration and approval routing, but the first lab run should confirm that `hyperv_host_status` reports it and that no approval dialog appears.

### 2026-10-29 — v0.2 phase 4: lab_run, lab_cycle and NMI injection

- `lab_run(vm_name, package_id, run_id, expected_manifest_sha256, timeout_seconds=600, checkpoint_before=true)` is the autonomous iteration the lab profile exists for. It reads `vm_get` and refuses unless the VM is already `Running`, creates checkpoint `pre-<run_id>`, re-verifies the package through `Test-DriverLabPackage.ps1`, then runs `Invoke-DriverLabRun.ps1` with the approval phrase `EXECUTE <package_id> <run_id>` generated by the server. Both identifiers are validated as `[A-Za-z0-9][A-Za-z0-9.-]{0,63}` before they are interpolated, so no quoting can break out of the guest command.
- The run is synchronous on purpose. The guest worker blocks for its length — which is what a run is — while `vm_get`, console and dashboard calls keep answering on the host worker. `service.guest_exec` grew a keyword-only `timeout_seconds` so the run's own deadline (10–3600 s) bounds the guest call instead of the 90 s worker default.
- Outcome classification uses the VM state and uptime, not just the exit code: `ok` and `failed` come from `C:\DriverLab\Runs\<run_id>\result.json`; `not_verified` means the package did not match its manifest and nothing was executed; `bugcheck` means the guest stopped answering **and** `vm_get` reports a lower uptime than the pre-run baseline, which is what a reboot looks like; `hang` means the guest stopped answering while the VM kept running; `timeout` means the deadline expired with the guest alive. A `bugcheck` report also tries to read the System log's BugCheck (event 1001) entries, best effort, because the guest may be gone.
- `lab_cycle` composes `lab_run`, evidence collection (kernel dump included by default) and an optional `checkpoint_restore` of `pre-<run_id>`. Evidence and revert failures are recorded in the report instead of raised, so a bugchecked run still returns its classification.
- `vm_inject_nmi` wraps `Debug-VM -InjectNonMaskableInterrupt` for the hang case: turning a hang into a crash is the only way to get a dump from it. It requires `HYPERV_MCP_ALLOW_DEBUGGER`, an exact `confirm_vm`, and a Running VM.
- `lab_run` and `lab_cycle` are refused outside `HYPERV_MCP_PROFILE=lab`. That is the security boundary: the approval phrase is never typed by the caller, so the profile — a deliberate operator setting — is the decision to let a loop execute staged packages.
- Local validation: 227 passing tests (15 new in `tests/test_lab_run.py`: ordering, verification gate, each classification, argument validation, evidence and revert composition, the profile gate and the NMI policy), clean Ruff, all PowerShell files parse, PSScriptAnalyzer reports no errors or warnings on `src`.
- Not yet validated live: no package has been executed in a real guest. The synchronous design assumes `Invoke-Command -Session` survives a run longer than 90 s, that a PowerShell Direct session with admin credentials passes the runner's `IsInRole(Administrator)` check, and that `vm_get`'s `uptime_seconds` moves the way the bugcheck rule assumes after a real crash. `Debug-VM -InjectNonMaskableInterrupt` and the BugCheck event query are likewise untested against Hyper-V.

### 2026-10-29 — v0.2 phase 4: lab_guest_configure

- `lab_guest_configure(vm_name, confirm_vm, testsigning, kernel_debug, crash_dump, disable_hvci, secure_boot_off, restart)` prepares a guest for driver work. `testsigning` and `kernel_debug` are BCD entries (`bcdedit /set {current} …`), `crash_dump` sets `CrashDumpEnabled`/`AutoReboot`/`Overwrite`/`AlwaysKeepMemoryDump` under `CrashControl`, and `disable_hvci` turns off both `EnableVirtualizationBasedSecurity` and the HVCI scenario `Enabled`. Every one is recorded in `guest-config.json` — BCD values in a new `bcd` list that remembers whether the entry existed and what it was — so `guest_config_revert` undoes them too.
- The toggles keep the tri-state convention of the automation settings, and `crash_dump` is a mode rather than a toggle: `kernel`, `complete` or `none` applies it and `false` restores the mode it replaced.
- Secure Boot is checked first through a new `vm_firmware` bridge operation (`Get-VMFirmware`; generation 1 reports `secure_boot_supported: false`). If it is On, test signing and kernel debugging are refused, because the guest would ignore them. `secure_boot_off=true` disables it through a new `vm_secure_boot` operation — which the bridge only allows while the VM is Off — and then returns without touching the guest, because the guest settings need it Running. The tool never changes power state itself.
- The pagefile is reported rather than resized: `driver_ready.dump_support` gives memory, pagefile size and whether the pagefile covers RAM plus about 257 MB, which is what a complete dump needs.
- `restart=true` reuses the graceful shutdown/start/interactive-session path from `guest_config_revert` and still requires `HYPERV_MCP_ALLOW_POWER`.
- Local validation: 233 passing tests (6 new in `tests/test_service.py`, 2 in `tests/test_guest_config.py`), clean Ruff, all PowerShell files parse, PSScriptAnalyzer reports no errors or warnings on `src`.
- Not yet validated live: `bcdedit` output parsing, `Set-VMFirmware -EnableSecureBoot Off` on a real generation 2 VM, and whether a guest actually honours `CrashDumpEnabled` and the HVCI registry values after a reboot are all untested. The BCD regex assumes the `bcdedit /enum {current}` layout of Windows 10/11.

### 2026-10-29 — v0.2 phase 4: console screenshots without VMConnect

- New bridge operation `vm_thumbnail` wraps `Msvm_VirtualSystemManagementService.GetVirtualSystemThumbnailImage` and returns the raw RGB565 bytes, base64-encoded, with the width, height and the stride the payload implies. The request is sized to the video head from `console_info` (`Msvm_VideoHead.CurrentHorizontalResolution/VerticalResolution`), so the image is the same coordinate space `console_mouse_move`/`SetAbsolutePosition` uses.
- `console_screenshot` gained `source`: `"thumbnail"` (the default) needs no VMConnect window, no interactive session and no visible desktop; `"vmconnect"` keeps the previous `PrintWindow` capture. Metadata now says `coordinates_match_mouse`, true only when the image was not downscaled, and the VMConnect path says false because window chrome, scaling and letterboxing break the correspondence.
- RGB565 is decoded with Pillow's `BGR;16` raw mode (red in the high five bits, blue in the low five). Pillow scales the 5/6-bit channels to the full 0–255 range, so pure red decodes as 255 rather than 248; `decode_rgb565` accepts a stride and refuses a payload too short for the requested size instead of cropping it.
- `console_wait_for_change(vm_name, timeout_ms=30000, region=[x, y, width, height], min_change=0.01)` polls a 160-pixel-wide thumbnail, reduces it (or the requested region) to a 64x36 greyscale sample and reports the fraction of pixels that moved by more than 8 grey levels. It always samples at least once, even when the timeout is shorter than the 0.4 s poll interval, and returns `changed`, `timed_out`, `polls`, `change_fraction` and `elapsed_ms` — never an image.
- `console_batch` is now one bridge request carrying the whole ordered action list instead of one request per action, so another console call cannot interleave with the middle of a batch. `wait` actions run inside the bridge, which means the host worker is busy for the batch; the existing 30 s cumulative wait budget bounds that. `click` sends the numeric `button_index` the WMI method expects.
- `Msvm_Keyboard` and `Msvm_SyntheticMouse` instances are cached per VM Id and class in the worker. The instances live as long as the VM object, so a guest reboot keeps them valid; a re-created VM has a new Id and therefore a new key, and the cache is cleared at 64 entries.
- The shared typing and key-chord helpers (`Send-KeyboardText`, `Send-KeyChord`, `Move-MousePointer`, `Invoke-MouseClick`, `Invoke-MouseScroll`) are used by both the single-action operations and the batch, so the two paths cannot drift.
- Local validation: 250 passing tests (9 new in `tests/test_capture.py`, 8 in `tests/test_service.py`), clean Ruff, all PowerShell files parse, PSScriptAnalyzer reports no errors or warnings on `src`.
- Not yet validated live: `GetVirtualSystemThumbnailImage` has not run against a real VM, so the RGB565 byte order, the row padding (the stride is inferred, not requested), whether the returned image really is the video head's resolution when the guest runs a different mode, and how often Hyper-V refreshes the thumbnail are all assumptions. The thumbnail may also be unavailable or black while the VM is Off. `console_wait_for_change`'s 1% change threshold and 0.4 s cadence are guesses about a real desktop; they will need tuning against `NORTH-Test` (cursor blink, clock, animations).

### 2026-10-29 — v0.2 phase 5: the kd.exe session engine (architecture decision)

The roadmap asked for an explicit decision between reusing [`mcp-windbg`](https://github.com/svnscha/mcp-windbg) and writing a module of our own. **Decision: our own `src/hyperv_console_mcp/kd/` module.**

- `mcp-windbg` 1.6.0 (MIT) is a complete MCP server for crash-dump analysis: its own stdio process, its own tool names, its own configuration, and no knowledge of Hyper-V, VMs, named pipes, KDNET, the allowlist, capability flags, dashboard approval, audit records or events. Reusing it "as a service" would mean a second MCP server the host has to launch and proxy through, and vendoring it would mean carrying a second policy layer that does not share ours. Either way a `kd_*` call would bypass `HYPERV_MCP_ALLOWED_VMS`, `HYPERV_MCP_ALLOW_DEBUGGER`, `confirm_vm`, the audit log and the event stream — the one thing this project does not allow.
- What was worth taking was the *engineering*, not the code, and it is credited in `kd/session.py`: a per-session random nonce inside the end-of-command marker (so a late reply to an abandoned command can never be read as the current one), a separate path for the commands that resume the target (`g`, `gh`, `gn`, `gc`, `gu`) which must never wait for a marker, and `CTRL_BREAK_EVENT` sent to a child created in its own process group as the break-in.
- The engine is small enough to own: `discovery.py` finds `kd.exe`/`cdb.exe` (`HYPERV_MCP_DEBUGGERS_DIR`, then `PATH`, then the Windows Kits directories — never WinDbgX, whose CLI differs), `session.py` drives one process, `analyze.py` parses `!analyze -v`, `paths.py` keeps the per-VM state and logs, `manager.py` owns at most one session per VM, `kdnet.py` does the host-side KDNET setup.
- End-of-command detection is a marker, never a timer: `.echo HVMCPKD<nonce><seq>` before and `...END` after the command, and the reader waits for the end marker. A timeout only breaks into a *running* target before reporting `kd_command_timeout`, so a slow `!analyze -v` cannot leave the session half-parsed.
- The state machine is explicit and reported by `kd_status`: `disconnected → waiting_for_target → running ↔ broken`, plus `disconnected` again after `qd` or an exit of kd.exe. `kd_go` refuses unless the target is `broken`; `kd_wait_event` classifies `break`, `bugcheck`, `module_load`, `exited` or `timeout`.
- Output is truncated at `HYPERV_MCP_MAX_OUTPUT_CHARS` keeping 60% of the head and 40% of the tail, with an explicit `[... N characters omitted ...]` marker, and the whole session is appended to `%LOCALAPPDATA%\HyperVConsoleMCP\kd\<vm>\<timestamp>.log` regardless of truncation.
- `_NT_SYMBOL_PATH` is `srv*<HYPERV_MCP_SYMBOL_ROOT>*https://msdl.microsoft.com/download/symbols`, plus any staged lab package directory that actually contains a `.pdb`, so a private symbol next to a staged driver resolves without a second setting.
- Local validation: 71 new tests across `tests/test_kd_analyze.py` (6), `tests/test_kd_session.py` (25, against `tests/fake_kd.py`, a fake debugger that speaks the same prompt/marker protocol), `tests/test_kd_discovery.py` (3), `tests/test_kd_manager.py` (8), `tests/test_kdnet.py` (11) and `tests/test_kd_service.py` (18).
- Not yet validated live: no kd.exe has been attached to a real target. The marker round-trip, the prompt regex, `CTRL_BREAK_EVENT` on a kd child, the `-b` initial break and the KDNET `-k net:port=…,key=…` command line are all untested against Microsoft's debugger; the fake only proves the protocol the engine expects, not that kd speaks exactly that protocol.

### 2026-10-29 — v0.2 phase 5: kd tools, KDNET setup, and a debugger in the lab cycle

- Sixteen new tools. Session: `kd_attach`, `kd_status`, `kd_detach`, `kd_break`, `kd_go`, `kd_command`, `kd_wait_event`, `kd_set_breakpoint`, `kd_clear_breakpoints`, `kd_break_on_load`, `kd_stack`, `kd_modules`, `kd_analyze`, `kd_dump_open`. KDNET: `debug_kdnet_status`, `debug_kdnet_prepare`, `debug_kdnet_deprovision`. `kd_command` and every other mutating call is annotated destructive; `kd_status`, `kd_wait_event`, `kd_stack`, `kd_modules`, `kd_analyze`, `kd_dump_open` and `debug_kdnet_status` are read-only. Every mutating `kd_*` is in `GATED_TOOLS`, so one dashboard approval covers the whole tool call and the lab profile skips it.
- `debug_kdnet_prepare(vm_name, confirm_vm, port=None, use_existing_adapter=false)` reuses the Phase 2 internal switch (`HyperV-MCP-Lab`, never an external one), allocates the lowest free UDP port in 50000–50039 by reading the sibling `kdnet.json` files, generates the key on the host, writes it into `bcdedit /dbgsettings net hostip:<ip> port:<p> key:<k>` through PowerShell Direct, and only then creates a host firewall rule for `kd.exe`, that UDP port and the `vEthernet (HyperV-MCP-Lab)` interface. The key is stored in the Windows Credential Manager under `HyperVConsoleMCP/kdnet/<vm>` before the guest call and deleted again if the guest refuses; it never appears in a result, an event or the audit log. The guest command masks `key=…` before any output can travel back, and `guest_dbgsettings` is returned masked.
- `kd_attach(transport="kdnet"|"serial")` refuses without prepared KDNET state and a stored key, or without a COM1 pipe and a Running VM. A `serial_connect` reader on the same pipe is detached first and reported as `serial_detached`, and `serial_connect` now refuses while a kd session owns the pipe — one reader per pipe, in both directions.
- `kd_break(nmi=None)` tries the console break-in and falls back to `vm_inject_nmi` when the target does not stop; `nmi=false` disables the fallback and `nmi=true` skips straight to it. The method used is reported.
- `lab_cycle(..., debugger="none"|"serial"|"kdnet")` attaches kd before the run and asks the session what happened afterwards. A bugcheck is then reported as `outcome="bugcheck"` with `debugger.analysis` holding the parsed `!analyze -v`, instead of being guessed from uptime as a hang. The session is left attached so the agent can `kd_stack`/`kd_modules` before `kd_detach`.
- `kd_dump_open(vm_name, dump_path, commands=None)` runs `kd -z` on the host against a dump confined to `HYPERV_MCP_EVIDENCE_ROOT` and ending in `.dmp`, and returns both the raw output and the parsed report.
- New bridge operations `kdnet_firewall_apply` and `kdnet_firewall_remove`. The rule name must match `HyperVConsoleMCP KDNET <vm>`, the port must be in 50000–50039, the program must exist and the interface alias must resolve before anything is created; re-applying removes the old rule first so re-running prepare cannot stack duplicates.
- Local validation: 324 passing tests (74 new: 71 kd tests, 3 in `tests/test_lab_run.py`, plus the server schema assertions), clean Ruff, all PowerShell files parse, PSScriptAnalyzer reports no errors or warnings on `src`.
- Not yet validated live: KDNET has not been provisioned on a real VM, so the internal switch address, the guest `bcdedit` call, the firewall rule and a real kd connection are untested end to end; `New-NetFirewallRule -InterfaceAlias` has not been exercised on this host. The `!analyze -v` parser was built from a real d1 bugcheck block but only tested against fixtures. `kd_dump_open`'s `-z` invocation is tested through a fake process, not a real dump.

### 2026-10-29 — v0.2 phase 6: tests, CI and documentation

- `tests/test_secrets.py` (6 tests) is the Definition-of-Done test: it drives the real code paths for the three secrets this server handles — the guest password, the KDNET key and the SSH private key — and scans every output for the test values. Scenarios: `debug_kdnet_prepare`/`status`/`deprovision` through a real `HyperVService` with a fake bridge and a fake vault, the `kdnet.json` state file, `kd_attach` against a real `KdSession` running `tests/fake_kd.py` (the key is asserted to be in the argv, then shown absent from the result, `kd_status`, `kd_command`, `kd_stack`, `kd_modules` and the session log), `guest_exec` wrapped in an event operation so the daily JSONL log is written and scanned, `guest_credential_status`, and `guest_ssh_status` with a private key on disk. Each test also scans the audit log; the SSH one scans the whole state tree except the key file itself.
- `tests/mcp_smoke.py` asserted **44** tools — stale since phase 2 and a real CI break. It now asserts 78, matching the README table, and the workflow runs it on every push. `.github/workflows/tests.yml` also builds the `v0.2-autonomous-lab` branch, so the work lands in CI instead of only on `main`.
- New live scripts, none of which run in pytest: `tests/live_kd.py` (KDNET or serial attach → status → break → stack → command → optional go → detach, with `--deprovision`), `tests/live_guest_automation.py` (`--configure`, `--check` writing a thumbnail PNG, `--revert`) and `tests/live_cycle.py` (stage a package whose `Run-Lab.ps1` only writes a file, then `lab_cycle` with checkpoint, evidence and revert). All read `HYPERV_MCP_LIVE_TEST_VM`.
- Docs: new `docs/WINDBG.md` (transport choice, session model, all 21 debugging tools, KDNET and serial setup, symbols, bugcheck flow, troubleshooting), `docs/SSH.md` (selection, provisioning steps, pinning, teardown, when not to use it) and `docs/GUEST-ACCESS.md` (Credential Manager, LSA autologon, the toggles, the revert path, the screenshot check). `docs/DEBUGGING.md` is now a pointer to `WINDBG.md` instead of a second, staler copy. `docs/SETUP.md` gained a kernel-debugging section and links; `README.md` gained the live-script table and the new doc links.
- `SECURITY.md` now states the threat model explicitly: the approval gate bounds what a *tool call* may do and is not a sandbox, it does not protect against an agent with a shell on the host, and the lab profile deliberately hands an autonomous loop full control of the allowlisted VMs. The credentials section describes the Credential Manager as the primary store, the SSH key ACL and the KDNET key rules; the capture section describes the thumbnail path as the default.
- Local validation: 330 passing tests (6 new), clean Ruff, all PowerShell files parse, PSScriptAnalyzer reports no errors or warnings on `src`, and the MCP stdio smoke passes locally with 78 tools.
- Not yet validated live: none of the three new live scripts has been run — there was no disposable VM and no elevated session in this delivery, so `live_kd.py` (KDNET and serial), `live_guest_automation.py` and `live_cycle.py` are written but unexercised. The secrets test proves the *code paths it drives* do not leak; it has not been run against a deliberately leaking build as a negative control.

### 2026-10-29 — v0.2 phase 6: break-on-load from the package manifest

- Closes the last roadmap item of §6.5 that was still missing: `lab_cycle(..., debugger=...)` now arms `sxe ld:<driver>` before the payload runs, not only after a bugcheck.
- `lab/run.py` grew `_driver_modules()`, which reads `C:\DriverLab\Packages\<package_id>\lab-manifest.json` from the guest through `guest_read_file` and returns the `.sys` stems (deduplicated, case-folded on the extension, `PurePosixPath(...).stem` because manifest paths are POSIX-separated). A missing, unreadable or malformed manifest returns `[]` rather than failing the cycle — the manifest is a hint, not a precondition, and `lab_verify` is what enforces it.
- `_arm_driver_breakpoints()` calls `kd_break_on_load` per module and reports `{"modules", "armed", "error"}`. A failure stops arming the rest and records the error code, but the run still proceeds: losing a breakpoint is worse than losing the whole iteration, and the report says which modules did not get one.
- Ordering matters and is asserted: the breaks are armed right after `kd_attach` and before `run()` executes anything, so the target stops at `DriverEntry` instead of racing the first break-in.
- Local validation: 332 passing tests (2 new in `tests/test_lab_run.py` — one driver plus a nested `sub/Helper.sys` plus a non-`.sys` file, asserting both the `break_on_load` report and `order.index("kd_break_on_load") < order.index("run")`; and a driverless package asserting nothing is armed). Clean Ruff.
- Not yet validated live: no real package has been run against a real target, so `sxe ld:<module>` has never actually been issued to kd.exe. The fake only proves the server sends the stem in the right order.

### 2026-10-29 — live validation on `NORTH-Test`: the lab cycle

- `tests/live_cycle.py` ran a full `lab_cycle` on `NORTH-Test` (stage → verify → checkpoint → run → evidence → revert) and `tests/live_lab_publish.py` published a package and then had one rejected. Both exited 0: `outcome: "ok"`, `revert.restored: true`, and the rejected package (`extra.dll` present but not in the manifest) was refused with `extra.dll` named and nothing left in `Packages\`.
- Four defects only the live guest showed, each with a test:
  - the guest wrote `result.json` with `Set-Content -Encoding UTF8`, which emits a BOM that `json.loads` rejects, so every run looked like a hang. The guest now writes it BOM-less and the host reads `utf-8-sig`, so a file written by the older build still parses.
  - the guest's ExecutionPolicy is the operator's, and `& 'C:\DriverLab\Tools\…'` was refused under it. Every server-owned guest script now goes through `guest_script()`, a `powershell.exe -ExecutionPolicy Bypass -File` call scoped to that one file.
  - `initialize()` raised when an installed lab tool differed from the packaged one, which stranded a guest on the first version it ever received. It now replaces it and reports `installed` and `updated`.
  - `_restore` looked the checkpoint up by name and could miss one created moments earlier; `cycle` now passes the id `create_checkpoint` returned.
- Not validated live: the driver in the staged package was a placeholder, so `sxe ld:<driver>` and the break at `DriverEntry` are still only exercised by tests.

### 2026-10-29 — live validation on `NORTH-Test`: the automation toggles

- `tests/live_guest_automation.py --configure` (exit 0) reports `managed_entries: [lock_screen, autologon, windows_update, uac, power, disable_cad]`, `lock_screen_disabled: true`, `uac_consent_prompt_for_admins: 0`, `windows_update_disabled: true`, power `standby/monitor/console_lock = 0`, `screensaver_active: "0"` and `autologon {auto_admin_logon: "1", default_user_name: "mcn1", password_in_registry: false, password_in_lsa: true}`. An interactive session `mcn1 console 1 Ativo` exists, and the captured thumbnail is a real Windows 10 desktop with no login prompt and no lock screen — the Phase 3 Definition of Done.
- The live script was passing `False` for the toggles, which in the tri-state API means "restore the previous value", so it was asserting nothing. It now passes `True` and compares the guest's own post-apply view with what the call promised, so a toggle that silently fails to stick is a failure.
- Two defects fixed: the state file came back as `PSCustomObject`s and `Get-EntryRecord` extends a record with `$record[$field] = @()`, which throws "cannot index into a PSObject" — the first Apply worked and every Apply after it failed (recursive `ConvertTo-Hashtable`); and the power reader matched English `powercfg` labels, which a pt-BR guest never prints, so `standby_timeout_ac` came back `None`. It now reads `Win32_PowerSettingDataIndex` by setting GUID, which is language-neutral.
- `guest_config.ps1` can be dot-sourced by those tests: `-Mode` is validated in main instead of being `[Parameter(Mandatory)]`, and the main block is guarded.
- Not re-run in this batch: `guest_config_revert`. The toggles are still applied on `NORTH-Test`; reverting them is a deliberate, separate action.

### 2026-10-29 — live validation on `NORTH-Test`: kd.exe over the COM1 pipe

- `tests/live_kd.py --transport serial` (exit 0) attached Microsoft's `kd.exe` to `NORTH-Test` through the COM1 named pipe: `kd_attach` reached `waiting_for_target`, `kd_break` broke in (`Break instruction exception - code 80000003`), `kd_stack` (`k 12`) returned the idle stack, `kd_command lm` listed modules with `ntkrnlmp.pdb` resolved from the symbol server, and `kd_detach` left the session `disconnected`.
- This exercises the marker protocol, the prompt regex, `CTRL_BREAK_EVENT`, the state machine and `_NT_SYMBOL_PATH` against the real debugger rather than `tests/fake_kd.py`.
- Still not validated live: KDNET (`debug_kdnet_prepare` needs a guest reboot, so `--transport kdnet` was not run) and `kd_dump_open` against a real dump.

### 2026-10-29 — live validation on `NORTH-Test`: SSH end to end

- `tests/live_ssh.py --use-existing-adapter --deprovision` now exits 0: status → provision (`verified: true`) → `guest_exec` over SSH (`DESKTOP-AA9PSTJ`) → `guest_status` over SSH (`windows_version: 10.0.19045`) → status (`provisioned: true`) → deprovision (`state_removed: true`, firewall rule and key gone, the guest's lab address removed, the reused adapter put back on `Default Switch`).
- Six defects only a real guest showed, each with a regression test:
  - only `provision` shipped `guest_ssh_setup.ps1`; `status` and `deprovision` ran it by path and failed with "the argument … does not exist". All three modes upload it first.
  - `_adapter_mac` read `adapters`; the bridge emits `network_adapters`, so a healthy adapter was reported as having no MAC — after the host had already been changed.
  - a failed provision left the VM on the lab switch with no sshd, and the deprovision that followed disconnected its only adapter. `provision` now undoes the host changes on every failure path, and `ssh_network_remove` no longer disconnects an adapter when it has nothing to restore (`connection-kept`).
  - the guest has no internet, so `Add-WindowsCapability` failed with `0x8024402c`. The host now downloads the pinned Win32-OpenSSH release (`10.4.0.0p1-Preview`, size and SHA-256 fixed in `ssh_setup.py`), verifies it, extracts it refusing any member that escapes the directory, ships it over PowerShell Direct and retries once, driven by a distinct `HVMCP_NEEDS_PACKAGE:` marker.
  - `icacls … /grant:r 'Administrators:F' 'SYSTEM:F'` fails on a pt-BR guest. The grants now name the well-known SIDs (`*S-1-5-32-544`, `*S-1-5-18`) and the error carries icacls's own output.
  - `config_lines` were `Get-Content` objects decorated with `PSPath`/`PSDrive`/`PSProvider`; `ConvertTo-Json` walked that graph and two lines serialized to 728 KB, so the result was cut at the 65536-character output limit and `guest_ssh_status` died with "Invalid base64-encoded string: number of data characters (65525) …". The lines are cast to string, and a cut result now says it was cut instead of reporting a base64 error.
- Two smaller findings: `Get-SetupState` reported `firewall_remote_address: ''` for a rule that really was scoped to `10.77.0.1` — a rule's own `RemoteAddress` is empty and the scope lives on its address filter; and `SshTransport.health` sent `powershell -Command "$os = …"`, which the guest's sshd shell (itself powershell.exe) parsed first and expanded to nothing, so `windows_version` was `null` over SSH while PowerShell Direct reported it. Health and the guest hash probe now use `-EncodedCommand`.
- `paramiko>=3.4,<4` is the declared `[ssh]` extra and is installed in the development interpreter; the `auto`/`psdirect` paths never import it.
- Not validated live: a guest that has to *create* the lab adapter and the host address (this VM already had both, so `adapter_created` and `host_address_created` were false), and `connection-kept` (the deprovision runs both recorded `previous_switch: "Default Switch"` and restored it, so the no-record branch is covered only by the bridge test). The pinned-package retry itself is validated: on the first attempts `Add-WindowsCapability` failed with `0x8024402c`, the host shipped the archive, `install-sshd.ps1` registered the service, and the run then failed one step later at the key file's ACL.
- Local validation for the whole batch: 358 passing tests, 1 skipped, clean Ruff, all 10 PowerShell files parse, PSScriptAnalyzer reports no errors or warnings on `src`.

### 2026-10-29 — v0.3 phase 1: correctness fixes

Five defects, each with a regression test that fails on the previous code.

- **Crash detection was blind to its own uptime type.** The bridge reports `uptime_seconds` as `[Math]::Round(...)`, which is always a float, but the baseline and the classifier both tested `isinstance(uptime, int)`. Neither ever matched: the baseline stayed at 0 and a crash plus reboot was classified as a hang. Both now accept any real number except `bool`.
- **A crash was classified too early and from the wrong source.** While the guest writes its dump the VM still reports `Running` with a high uptime, which also looked like a hang. The classifier now polls the VM state a few times for the reboot to land, and prefers the crash record the host writes — event 18590 in `Microsoft-Windows-Hyper-V-Worker-Admin`, whose bugcheck code and parameters are numeric template fields and therefore language-neutral. The guest's own event 1001 stays as a fallback, because it exists only after the guest has rebooted far enough to write it. A crash recorded before the run started does not count, and `bugcheck_source` says which record was used.
- **A long guest script overflowed the Windows command line.** SSH sent everything through `powershell -EncodedCommand`, which costs about 2.7 command-line characters per script character against a 32 767-character ceiling, so calls failed near 8 KB of command and 18 KB of write payload while the API accepts 131 072. A script whose encoded form would exceed 28 000 characters is now uploaded by SFTP with a UTF-8 BOM (PowerShell 5.1 reads a script as UTF-8 only when the BOM is there) and run with `-File`, then deleted on the way out — including when the run fails.
- **`auto` did not recover when SSH died mid-run.** The 30 s health cache still said "healthy" after a guest reboot, so the following calls kept going to a dead session. A connection-class failure (`ssh_unavailable`/`ssh_error`) in `auto` now marks the transport down and repeats the operation once over PowerShell Direct, recording a `guest_transport` fallback in the audit log. An explicit `ssh` still fails rather than falling back silently.
- **The BCD reader compared the wrong token, not the wrong language.** The roadmap expected `bcdedit` to translate its values (`Sim`/`Não` on a pt-BR guest). It does not: only the labels around an entry are translated, and a live pt-BR guest prints `testsigning             Yes`. The real defect was that the reader compared that token against `'on'`, which `bcdedit` never prints, so test signing and kernel debugging read back as off on every guest in every language, even when both were on; `guest_config_revert` would also have written `Yes` back as a value `bcdedit` rejects. `root\wmi` `BcdStore` was tried as the language-neutral replacement the roadmap suggested and proved unusable on this host (`OpenStore('')` succeeds, but `OpenObject('{current}')` returns false and `EnumerateObjects` fails with an invalid-method-parameter error), so `bcdedit` stays and the tokens are normalised to `on`/`off` — which is also what `bcdedit` accepts as input on a localized guest, verified live (`on`/`off` accepted, `Sim` rejected).

Live validation on `NORTH-Test`:

- `tests/live_large_payload.py --provision-ssh --use-existing-adapter --deprovision` exited 0. Over PowerShell Direct and then over SSH it ran a 99 217-character `guest_exec` (marker returned, `success: true`), uploaded a 102 400-byte file and read it back, with the SHA-256 matching both times; SSH was provisioned, verified and deprovisioned in the same run.
- The BCD check (`lab_guest_configure(testsigning=True)` → status → `guest_config_revert`) exited 0 on the pt-BR guest: before `test_signing: false`, `kernel_debug: true` (the `true` is the fix — the old reader said `false`), after apply `test_signing: true`, after revert `test_signing: false` with `kernel_debug` still `true`. `secure_boot` reported `Off`.
- The live BCD run also surfaced an unrelated bridge defect: `vm_firmware` read `$firmware.PreferredBootDevice` directly, and strict mode turns the property's absence into a fatal error, so every `lab_guest_configure` call died before reaching the guest. It now reads the property through `PSObject.Properties` and reports `null` when the host omits it.

Not validated live: the crash classifier. No bugcheck was induced in this phase, so event 18590, the state polling and the `bugcheck`/`hang` split are covered only by unit tests; the earlier live evidence is the v0.2 cycle, which did not crash. The `auto` fallback is likewise exercised only against fakes — no real SSH session has been killed in the middle of a `lab_cycle`.

Local validation: 372 passing tests, 1 skipped, clean Ruff, all 10 PowerShell files parse, PSScriptAnalyzer reports no errors or warnings on `src`. The tool surface is unchanged (78 tools), because phase 1 changed behaviour rather than tools.

### 2026-10-29 — v0.3 phase 2: a tool surface sized for a conversation

Every schema a conversation never uses is a cost it pays anyway, so the server no longer
advertises all of them. The surface went from 78 tools to 53, and a profile registers only
its own families.

- **Toolsets.** Eleven families — `core`, `guest`, `console`, `config`, `credentials`,
  `media`, `serial`, `ssh`, `lab`, `kd`, `debug` — assigned by tool-name prefix. A profile
  picks the default set, `HYPERV_MCP_TOOLSETS` replaces it outright (`all` means every
  family), and an unknown name is refused at startup instead of silently ignored. A tool
  outside the enabled families is never registered, so its schema never reaches the model.
  The safe profile exposes 29 tools and the lab profile 40. `hyperv_host_status` now reports
  which families are enabled and which are off, because otherwise a missing capability
  surfaces as an unknown tool with no explanation.
- **The roadmap's example list was extended.** `core,console,lab,kd,ssh,media` leaves the
  guest transport and the guest configuration with no home, and one flat `core` would make it
  impossible to hide PowerShell Direct while keeping host control. `guest`, `config`,
  `credentials` and `debug` are separate families for that reason.
- **Consolidation, without losing a capability.** `console_input` carries the whole input
  vocabulary as an ordered list of discriminated actions and `kd_execution` /
  `kd_breakpoint` carry break/go and set/clear/on-load. The same pattern was applied to the
  other near-duplicates: `checkpoint` (create/restore/delete), `guest_report`
  (status/processes/services/events), `guest_credential` (rotate/forget), `kd_inspect`
  (stack/modules/analyze/dump), `debug_setup` (`transport` × `prepare`/`guest`/
  `deprovision`), and `vm_get(wait_for=...)` absorbing `vm_wait`. Read-only tools stay
  separate from mutating ones — `checkpoint_list`, `guest_credential_status` and
  `debug_status` survive as their own tools — so a listing never turns into an approval
  prompt.
- **Two bridge actions the batch already had but no tool exposed.** `console_batch` gained
  `key_state` and `mouse_buttons`, so a held modifier or an explicit button state is one
  action in the same request instead of a tool that does not exist.
- **Correction to the first cut of this phase.** The first version of the lab profile was
  `core,guest,console,lab,kd`: it could run a package with a debugger but not prepare one
  (`kd_attach` answers "Run debug_serial_prepare first", and that tool was not registered),
  and it could apply `lab_guest_configure` but not revert it. The lab profile now includes
  `config` and `debug`, paid for by the consolidations above, and a test asserts both.
- **The approval gate names tools, not operations.** `GATED_TOOLS` is the set of
  `MUTATION`-annotated tool names, and `_approve` is called with the tool name — the summary
  carries the specific action (`{"action": "restore", "snapshot_id": …}`) so the dialog still
  says what is about to happen. The audit log keeps the specific operation names.

Local validation: 390 passing tests, 1 skipped, clean Ruff. The stdio smoke asserts 29 tools
on the safe profile and 53 with `HYPERV_MCP_TOOLSETS=all`, with the disabled mutation blocked
on both. Not validated live: no tool was called against `NORTH-Test` in this phase, because
the change is to what is advertised, not to what the tools do.

### 2026-10-29 — v0.3 phase 3: one service, eleven modules

- **`service.py` became the package `service/`.** The 2,205-line module held 111 methods
  spanning eleven unrelated jobs. Each domain now lives in its own module —
  `vm`, `guest`, `ssh`, `credentials`, `guest_config`, `lab`, `console`, `media`,
  `serial_console`, `debug_setup`, `debugger` — and `service/__init__.py` composes them into
  the same `HyperVService`. Callers did not change: the MCP server, the dashboard, the lab
  runner and every live script still import one class from one place.
- **The shared policy helpers moved to `service/_base.py`.** `ServiceBase` holds `__init__`,
  the allowlist and capability checks (`_vm`, `_write`, `_confirm`), the audit wrapper
  (`_call`), the operation wrapper (`_run`), the approval seam (`_approve`) and the two
  local-action wrappers. Every domain inherits it, and the façade lists it last so a domain
  always wins in the MRO.
- **The bodies were moved mechanically, not retyped.** The split was generated by slicing
  each method's source range out of the old module, so the diff is relocations plus import
  lines. A comparison of `ast.unparse` output for all 111 methods against `HEAD` shows only
  three kinds of difference: relative imports going from `.` to `..` (the new depth),
  `lab_stage` losing a redundant function-local `from pathlib import Path`, and three
  `_tool(...)` lookups described below.
- **One deliberate indirection in the debugger domain.** Three tests substitute
  `hyperv_console_mcp.service.find_debugger`, `.KdSession` and `.run_dump` to run without
  kd.exe. Those names stay on the package, and `service/debugger.py` resolves them through it
  at call time (`_tool("find_debugger")`), so the package remains the single place that names
  the external debugger tools and a substitution is honoured on the next call instead of at
  import time.
- **One test had to read a package instead of a module.** `tests/test_events.py` walks the
  service source to prove every `GATED_TOOLS` entry has a real `_approve` call site; it now
  walks `service/*.py`. The assertion is unchanged. No other test was edited — the suite is
  the same 390 tests, still green.
- **New: `tests/test_service_layout.py`.** Four structural tests pin what the split promised:
  the façade composes every domain with the base last, every method has exactly one home and
  the façade defines none of its own, no domain imports a sibling domain (only `._base`), and
  `GATED_TOOLS` is defined once.

Local validation: 394 passing tests, 1 skipped (390 unchanged plus the 4 layout tests), clean
Ruff, all 10 PowerShell files parse, PSScriptAnalyzer reports no errors or warnings on `src`,
and the stdio smoke still reports 29 tools on the safe profile and 53 with
`HYPERV_MCP_TOOLSETS=all`. Not validated live: this phase moved code without changing what it
does, so nothing new was exercised against `NORTH-Test`.

### 2026-10-29 — v0.3 phase 4: the host reads answer from WMI

- **`hostwmi/` is the Python path to the Hyper-V provider.** `connection.py` is the only
  module that imports `win32com`: it opens the locator per thread, connects to
  `root\virtualization\v2`, impersonates the caller, and flattens every object to a plain
  dict carrying `__PATH` and `__CLASS`. `host.py` and `vm.py` map those dicts; a fake
  connection exercises them without a hypervisor.
- **Migration is per operation, with the worker still answering the rest.** `WmiBackend`
  raises `wmi_unsupported` for anything not in `HANDLERS`, and the new `FallbackBackend`
  sends exactly that (and `wmi_unavailable`) to `PowerShellBackend`. So `host_status`,
  `vm_list` and `vm_get` now answer from CIM, and the other ~45 host operations keep going
  through the bridge untouched.
- **Nothing is matched by a display name.** The VM instances are the rows whose `Name` is a
  GUID (the host instance is named after the computer); `Caption` says "Máquina Virtual" on
  this pt-BR host, and the component classes call a disk "Disco Rígido". Disks, DVD drives
  and adapters are identified by `ResourceType`, `ResourceSubType` and the `InstanceID`
  segments; every enum (`EnabledState`, `UserSnapshotType`, `AutomaticStartupAction`, …) is
  translated from its number.
- **A permission-filtered host is not an empty host.** Without Hyper-V Administrator rights
  the provider returns no VM rows instead of failing. When the VM list comes back empty,
  `vm_list`/`vm_get` ask for `Msvm_VirtualSystemManagementService`; if that is empty too the
  answer is `wmi_unavailable`, so the fallback produces the same access-denied error the
  bridge gives today instead of a wrong "no VMs".
- **Queries name their properties.** The provider charges per property read: the
  `Msvm_VirtualSystemSettingDataComponent` association answered all 26 components with all
  70 properties and cost 144 ms, while six narrow `SELECT`s with an `InstanceID LIKE` filter
  cost about the same for a fraction of the data. `vm_get` went from 372 ms to ~190 ms that
  way. That is still slower than a *warm* PowerShell worker (45 ms), which is compiled and
  does the same WMI calls; the win is one less process, no localized parsing, and no 1.5 s
  cold start.

Live validation (elevated, `NORTH-Test` on this host): the WMI `vm_get` and `Get-VM` were
compared field by field — id, name, state, state_code, status, generation, version,
processor_count, memory figures, checkpoint_type, both automatic actions, checkpoint_count,
hard disks, DVD drives and network adapters — and **every field matched**, including the
localized status text, the DVD media paths, the switch name and the guest IP addresses. The
same call through `HyperVService` (allowlist and audit included) returned the same object in
190 ms. `host_status` over CIM also works unelevated, where the bridge needs elevation.

Not validated live in this slice: any checkpoint operation, DVD/COM change, synthetic
keyboard or thumbnail through WMI — those are still on the bridge.

### 2026-10-29 — v0.3 phase 4: power through RequestStateChange

- **`power.py` sends one `RequestStateChange` and then waits on the job.** The provider
  answers `0` when the transition finished inside the call and `4096` with an
  `Msvm_ConcreteJob` path when it did not; `await_job` polls that job to `JobState 7` and
  turns `8`/`9`/`10` into a `WmiFailure` carrying the provider's own `ErrorDescription`.
  `shutdown` and `save` stay on the bridge: they need a job that outlives the MCP call, and
  that registry has not moved yet, so they raise `wmi_unsupported` and the fallback takes
  them.
- **Two numbers were wrong until the live provider corrected them.** The first cut sent
  `32777` for `resume`, reading it off `EnabledState`; as a `RequestedState` the provider
  refuses it with `32775`. The method's `ValueMap` qualifier is the CIM set only — 2, 3, 4,
  6..11 — so resuming is the same request as starting (`2`), and the two actions differ in
  name only. The refusal table was wrong in the same way: the provider's codes are the
  documented 32768-32778 block, where `32774` is "the VM is in use", `32775` is "the VM is
  not in a state that allows this transition", `32776` is the data-type error and `32777` is
  "the VM is not available". Both corrections are pinned by tests.
- **Writing a WMI in-parameter takes the property set, not `setattr`.** pywin32's `gen_py`
  wrapper resolves attribute writes against the type library, and a parameter object from
  `SpawnInstance_()` has no such entries, so `setattr(in_parameters, "RequestedState", 9)`
  raises `KeyError` before anything reaches the provider. `connection.call` now writes
  `Properties_.Item(name).Value`, and the method is invoked as `ExecMethod` (no trailing
  underscore — that is a scripting name, not a Python-keyword escape).

Live validation (elevated, `NORTH-Test` on this host): `pause` returned `completed` with the
VM at `Paused` (9) and `resume` returned `completed` with it back at `Running` (2) — both
took the `4096` + job path and both were confirmed by a fresh `vm_get` afterwards. A resume
on a running VM answered `32775` and came out of the backend as `invalid_state` with the
provider's wording, and `shutdown`/`save` came out as `wmi_unsupported`, which is the
fallback trigger. Not exercised live: `turn_off`, `reset` and `start` from `Off`, which
would have restarted the guest for no extra coverage of the mapping.

Local validation: 449 passing tests, 1 skipped, clean Ruff, 11 PowerShell files parse,
PSScriptAnalyzer reports nothing on `src`, and the stdio smoke still reports 29 tools on the
safe profile and 53 with `HYPERV_MCP_TOOLSETS=all`.

### 2026-10-29 — v0.3 phase 4: checkpoints from the snapshot setting data

- **A checkpoint is an `Msvm_VirtualSystemSettingData` row.** Its `InstanceID` is
  `Microsoft:<snapshot id>` — the id `Get-VMSnapshot` reports, not a path under the VM's id
  — its `ElementName` is the name, and its `Parent` names the checkpoint before it, so the
  chain comes from the references rather than from a cmdlet walk. `checkpoint_list` maps
  those rows to the shape the bridge answered.
- **The timestamps needed a converter, not a pass-through.** WMI answers `CreationTime` as
  a CIM DATETIME in UTC (`20261029014417.778334-000`, where `-000` means "UTC, offset
  unknown"), while the bridge prints a local `DateTime` with round-trip precision. The
  mapper reads the offset in minutes, converts to the host's zone and writes seven
  fractional digits and `±HH:MM`, so the two paths produce the same string for the same
  instant — pinned by a test that only compares instants, so it holds in any time zone.
- **`checkpoint_create` stays on the bridge, deliberately.** The name travels in
  `SnapshotSettings`, an embedded-instance string, and the provider ignored every encoding
  tried on the live host: CIM XML with and without the prolog and the key property, the
  instance object itself (WMI refused the type), and the MOF of a cloned setting. Each
  attempt created a checkpoint named with the provider's own template
  (`NORTH-Test - (29/10/2026 - 15:52:30)`), so serving it from Python would silently drop
  the caller's name. `checkpoint_restore` and `checkpoint_delete` need no name and are
  served from here; `checkpoint_create` still answers `wmi_unsupported` and the fallback
  takes it, which is the migration working as designed.
- **`jobs.py` now holds the wait.** `RequestStateChange`, `ApplySnapshot` and
  `DestroySnapshot` all answer with the same `0`/`4096` + `Msvm_ConcreteJob` scheme and the
  same 3276x refusals, so `await_job` and the code table moved out of `power.py` and both
  operations share them. The messages say "operation" rather than "transition".

Live validation (elevated, `NORTH-Test` on this host): `checkpoint_list` was compared
against `Get-VMSnapshot` field by field for all 12 checkpoints — id, name, creation_time,
parent_snapshot_id and notes — and **every field matched**. `checkpoint_create` went through
the fallback to the bridge (a checkpoint named `wmi-delete-probe`), and `checkpoint_delete`
removed it from WMI with the count going 12 → 13 → 12. Not exercised live: `checkpoint_restore`,
which would revert the guest to a checkpoint, and the create path through WMI, which is not
served.

### 2026-10-29 — v0.3 phase 4: the console thumbnail from the provider

- **`GetVirtualSystemThumbnailImage` on the management service, not the bridge.**
  `thumbnail.py` asks for the caller's width and height, base64-encodes the `ImageData` byte
  array and infers the row stride from the payload length the way the bridge does — the
  provider can pad rows, and 640×480 came back as 614,404 bytes rather than 614,400, so
  `len // height` is the honest stride. A payload shorter than `width × height × 2` is an
  error rather than a silently cropped image, and a size outside 1..16384 is refused before
  the call, with the bridge's wording.
- **The provider's own return code is kept in the message.** The bridge's text
  (`GetVirtualSystemThumbnailImage returned 4 for 'NORTH-Test'.`) is reproduced exactly, so
  a caller that already parses it keeps working.

Live validation (elevated, `NORTH-Test` on this host): with the VM paused so both paths saw
the same frame, the WMI thumbnail and the bridge's were compared — `vm`, `state`, `width`,
`height`, `format`, `byte_length`, `stride_bytes` all equal, and the **decoded payloads were
byte-for-byte identical** (614,404 bytes). The VM was resumed afterwards and confirmed
`Running`. Not validated live: a thumbnail from a stopped VM, which no tool asks for.

### 2026-10-29 — v0.3 phase 4: the DVD drives from the resource settings

- **`media_list` reads two classes instead of six.** A DVD drive is an
  `Msvm_ResourceAllocationSettingData` of resource type 16 (`<vm>\<controller>\<number>\<location>\D`);
  the disc inside it is a separate `Msvm_StorageAllocationSettingData` of the virtual-CD
  subtype whose `Parent` names the drive. `vm.py` now builds its projected queries from one
  table, so `dvd_drives()` asks for those two classes and nothing else, and the drive mapping
  itself is shared with `vm_get` rather than copied.
- **`mounted` is added where it is reported, not in the shared mapping.** `Get-VM` shows a
  DVD drive as a slot and a path; `Get-VMDvdDrive` — what `media_list` shows — also says
  whether the tray holds anything, and the bridge treats a whitespace path as empty. The flag
  is therefore computed in `dvd_drives()` and `vm_get`'s shape is untouched.

Live validation (elevated, `NORTH-Test` on this host, VM running): `media_list` through WMI
and through the bridge were compared as whole dictionaries — `vm`, `state`, `generation` and
both drives with their `path` and `mounted` flags — and **the answers were equal**. Not
validated live: a VM with a DVD drive that holds no disc, which this VM does not have.

### 2026-10-29 — v0.3 phase 4: changing the disc is a detach and an attach

- **`ModifyResourceSettings` is not the call, and accepting is not doing.** The provider
  takes a `ModifyResourceSettings` for the disc setting and answers `0` without changing
  anything; given the new path it fails the job instead. What the host accepts is
  `RemoveResourceSettings` with the disc's object path followed by `AddResourceSettings` with
  a new disc setting for the same drive, so `media_mount` is those two calls in that order and
  `media_eject` is the first one alone.
- **The new setting travels as an embedded instance.** `AddResourceSettings` takes
  `ResourceSettings` as DTD 2.0 text, and `HostResource` is an array property: written as a
  plain `PROPERTY` the provider reads the path as a scalar and the job fails, so it is a
  `PROPERTY.ARRAY`/`VALUE.ARRAY` element. Only `ResourceType`, `ResourceSubType`, `Parent`
  and `HostResource` are sent — everything else is filled in by the provider, and the fields
  it would otherwise expect are localized text on a pt-BR host.
- **The disc is found by its `Parent`, and the query has to be scoped to the drive's slot.**
  A checkpoint carries its own copy of the same setting, so matching by slot alone would edit
  the checkpoint instead. The first version filtered the storage class by the drive's
  *object-path* prefix (`\HOST\root\virtualization\v2`), which no `InstanceID` starts with:
  the detach silently found nothing, the attach then failed with "the location is already in
  use", and the media never changed. The regression test asserts the `LIKE` is on
  `Microsoft:<vm>\<controller>\<number>\<location>\`.
- **`boot_first` stays on the worker.** It changes firmware rather than media (gen 2
  `FirstBootDevice`, gen 1 BIOS startup order), so the handler refuses with `wmi_unsupported`
  and the bridge serves the whole request — including the media change it would otherwise
  duplicate.

Live validation (elevated, `NORTH-Test` on this host): the VM was turned off, `media_mount`
mounted `virtio-win.iso` at 0/2 (read back as the drive's path), `media_eject` emptied it
(`path: null`), `media_mount` restored `NorthTestPackage.iso`, and the VM was started again
and confirmed `Running` with 0/1 and 0/2 back on their original media. Not validated live: a
`boot_first` request, which goes to the bridge, and a generation-1 VM.

### 2026-10-29 — v0.3 phase 4: the keyboard and the guest display size

- **One query per device, keyed by the VM's own GUID.** `Msvm_Keyboard`,
  `Msvm_SyntheticMouse` and `Msvm_VideoHead` each carry the VM's GUID in `SystemName`, so
  the device lookup is `SELECT * FROM <class> WHERE SystemName = '<guid>'`. The bridge walks
  the `Msvm_SystemDevice` association instead; on this host that query is refused outright
  (`Consulta inválida`) when `Antecedent` is compared to a path literal, and `SystemName`
  makes the walk unnecessary.
- **The keyboard methods answer directly.** `TypeText`, `TypeKey`, `PressKey`, `ReleaseKey`
  and `TypeCtrlAltDel` return a code instead of starting a job, so a non-zero answer is the
  failure — with the bridge's wording (`Hyper-V WMI method <name> returned <code>.`) so a
  caller that already parses it keeps working.
- **A chord releases what it pressed, even when a press fails.** Keys go down in order and up
  in reverse inside `finally`; a press that failed is not recorded, which matches the bridge
  and is correct — a refused press never put the key down.
- **`console_info` no longer costs a worker round trip**, which matters because every
  screenshot and every `console_wait_for_change` reads the display size first.

Live validation (elevated, `NORTH-Test` on this host, VM running): `console_info` through WMI
and through the bridge were compared as whole dictionaries — `vm`, `state` and the
`1024x768` video head — and **the answers were equal**. `console_key_state` down and up on
`shift`, `console_press_keys` with `shift`, and `console_type_text` with a five-character
string all returned `0` and reported what they sent. Not validated live:
`console_ctrl_alt_delete`, which would put the guest on its security screen, and a VM whose
keyboard device does not exist.

### 2026-10-29 — v0.3 phase 4: the mouse, the batch, and a bridge bug it exposed

- **`SetButtonState` takes `IsDown`, not `ButtonState`.** The provider answers the bridge's
  name with `SWbemPropertySet: not found`, so `console_mouse_buttons` and the
  `mouse_buttons` batch action had never worked — the bridge regression test pinned the
  broken call, so it passed. The Python path uses the real parameter, the bridge is fixed,
  and the test now pins `IsDown=True/False`.
- **The batch is one request for the same reason the bridge made it one.** `console_batch`
  looks each input device up once and runs the ordered actions against it, so an
  intermediate state cannot be interleaved with another console call; the per-action result
  shape is the bridge's, entry for entry.
- **`console_mouse_buttons` returns no `completed` flag.** The bridge's standalone handler
  does not either — its batch entry adds one — and the live comparison is what surfaced the
  difference.

Live validation (elevated, `NORTH-Test` on this host, VM running): `console_mouse_move` and
`console_mouse_buttons` through WMI and through the bridge were compared as whole
dictionaries and **were equal**; `console_scroll` (+120), `console_click` at 5,5 with the
left button, a press-and-release of the left button, and a five-action batch (move, wait,
`shift` down, `shift` up, buttons released) all returned `0` and reported every action. Not
validated live: a click that lands on a guest window, and a drag (which no tool offers).

### 2026-10-29 — v0.3 phase 4: the COM ports and the debugger tools

- **A serial port is an `Msvm_SerialPortSettingData` of resource type 21**, named
  `Microsoft:<vm>\<controller>\<index>` where the index is zero-based — the tool's COM number
  is that index plus one. The pipe or device is `Connection[0]`, and `DebuggerMode` is the
  flag `Get-VMComPort` shows as On or Off.
- **The class holds every VM's ports and every checkpoint's copy**, so the query is scoped by
  the VM's InstanceID prefix; the port order is imposed here because a WQL query has none.
- **An unconnected port reports `''`, not `None`.** The bridge's `$Port.Path` is the empty
  string and the live comparison caught the difference — a caller that compares the two
  answers would have seen it.
- **The tool probe is not WMI.** `windbgx` and `kdnet_installed` are two paths on the host,
  answered here instead of costing a worker round trip.

Live validation (elevated, `NORTH-Test` on this host, VM running): `debug_serial_status`
through WMI and through the bridge were compared as whole dictionaries — `vm`, `state`, both
COM ports (`COM 1` on `\.\pipe\north-test-kd` in debugger mode, `COM 2` unconnected) and the
three tool flags — and **the answers were equal**. Not validated live: a VM whose ports are
not in debugger mode on COM 1, which this VM's are.

### 2026-10-29 — v0.3 phase 4: pointing COM1 at a pipe

- **This one is a `ModifyResourceSettings`**, unlike the disc: the provider applies it. The
  setting travels as an embedded `Msvm_SerialPortSettingData` keyed by its `InstanceID`, with
  `Connection` as a `PROPERTY.ARRAY` and `DebuggerMode` as `TRUE`. Only the properties being
  changed are sent; the rest of the setting carries text the host has already translated.
- **The result is read back.** The disc taught that this provider answers a modify it did not
  apply with a success code, so the call re-reads COM1 and fails with `backend_error` if the
  path or the debugger flag did not take.
- **The pipe name is matched in full**, unlike the bridge's `-notmatch`, which only asks
  whether the string *contains* a valid name and therefore accepted `!!!abc`. The new check
  is stricter and the tool's contract is a pipe name, not a string with one inside.
- **A checkpoint's copy of the port is keyed by the checkpoint's identifier**
  (`Microsoft:<checkpoint>\<controller>\<number>`), not the VM's, so the VM-prefix query
  already excludes those; that is why no extra filtering was needed to find the realized port.

Live validation (elevated, `NORTH-Test`, stopped with `vm_power` and started again
afterwards): `debug_serial_prepare` through WMI and through the bridge were compared as whole
dictionaries on the same pipe name — `vm`, `port_number`, `pipe_path`, `debugger_mode`,
`baud_rate`, `windbg_arguments` and the four `next_steps` — and **the answers were equal**;
COM1 read back as `\.\pipe\north-test-kd-probe` in debugger mode, and was restored to
`\.\pipe\north-test-kd` before the VM was started. Not validated live: the refusal paths
(VM running, a bad pipe name) run only in the unit tests, and a host whose COM1 is not
controller 0.

### 2026-10-29 — v0.3 phase 4: the NMI, and the method that takes no parameters

- **`Debug-VM -InjectNonMaskableInterrupt` is one method on `Msvm_ComputerSystem` with no
  in-parameters at all**, so the operation needs no setting data and no embedded instance.
- **A parameterless method broke the COM boundary.** `ComConnection.call` asked for
  `InParameters.SpawnInstance_()`, and for this method `InParameters` is `None` — the call
  raised before it reached the provider. It now passes `None` to `ExecMethod`, and refuses
  a caller that supplies parameters to a method that has none.
- **The reported state is the one the call found.** A guest with no debugger attached usually
  bugchecks a moment after the NMI, so reading the state afterwards would race that.

Validated live: the method exists on this host's `Msvm_ComputerSystem` and reports no
in-parameters (introspection, read-only). **Not validated live:** the injection itself, which
would bugcheck `NORTH-Test` — that is a deliberate act for the operator, not a side effect
of a test run. The unit tests cover the refusal for a stopped VM and a provider refusal code.

### 2026-10-29 — v0.3 phase 4: the firmware, and the field that has no counterpart

- **`Get-VMFirmware` reads the realized setting**: `SecureBootEnabled` is the flag it prints
  as On or Off and `VirtualSystemSubType` carries the generation, so the operation is one
  projected query of two fields.
- **A generation 1 VM is answered, not refused.** The cmdlet errors on generation 1; the
  bridge reports `secure_boot_supported: false` with a null flag instead, and so does this.
- **`preferred_boot_device` has no WMI source on this host.** The cmdlet's own property list
  on this module carries no `PreferredBootDevice` at all (read from `Get-VMFirmware | Format-List *`
  with the VM running and five boot sources present), which is why the bridge's
  `PSObject.Properties[...]` lookup finds nothing and reports null. This reports null too, and
  it is the field to revisit on a host whose module does expose one.

Live validation (elevated, `NORTH-Test`, running): `vm_firmware` through WMI and through the
bridge compared as whole dictionaries — `vm`, `generation`, `state`, `secure_boot_supported`,
`secure_boot`, `preferred_boot_device` — and **the answers were equal** (generation 2, Secure
Boot Off). Not validated live: a generation 1 VM, and a VM with Secure Boot On; both are unit
tests here.

### 2026-10-29 — v0.3 phase 4: Secure Boot through ModifySystemSettings

- **The flag travels as a partial embedded `Msvm_VirtualSystemSettingData`** keyed by the
  realized setting's `InstanceID`, which is the same `ModifySystemSettings` shape `Set-VM`
  uses for memory and processor counts — no full configuration round trip.
- **The answer is read back, and `enabled` keeps what was asked.** The two can disagree, and
  the bridge reports both, so this does too.
- **Generation 1 is refused before the state check**, as the bridge does: the tool's contract
  is "generation 2 only", and a stopped generation 1 VM is still not a candidate.

Live validation (elevated, `NORTH-Test`, stopped with `vm_power` and started again): with the
VM Off, `vm_secure_boot` through WMI and through the bridge were compared on `enable: true`
and **the answers were equal** (`secure_boot: On`, `enabled: true`); `vm_firmware` confirmed
`Secure Boot On` while it was set, and the VM was returned to `Off` and started before the run
ended. Not validated live: the refusal paths (generation 1, VM running), which are unit tests.

### 2026-10-29 — v0.3 phase 4: creating a checkpoint is two calls, not one

- **`CreateSnapshot` takes no name.** Its `SnapshotSettings` parameter is a single string
  (CIM type 8, not an array — passing a list is refused with "Tipos incompatíveis"), and the
  provider ignored every embedded instance ever put in it: the checkpoint always came out as
  ``<vm> - (dd/mm/yyyy - hh:mm:ss)``. The name is applied afterwards with
  ``ModifySystemSettings`` on the new setting's ``ElementName``, which is what
  ``Rename-VMSnapshot`` does, and that call returns 0 with no job.
- **The rename belongs to the management service**, not the snapshot service — the snapshot
  service has no `ModifySystemSettings`.
- **`CreateSnapshot` names no id.** `ResultingSnapshot` comes back null, so the new checkpoint
  is found by comparing the listing before and after, waiting up to the same 300 s the bridge
  waits for `Checkpoint-VM -Passthru`.
- **The setting is re-read before it is reported**, because the row the wait found still
  carries the provider's default name.

Live validation (elevated, `NORTH-Test`, running): a checkpoint created through WMI and one
created through the bridge came back with the **same keys** and the same `notes`, the two
names the caller asked for, and parent ids chained one to the other — the fields that differ
(`id`, `creation_time`, `parent_snapshot_id`) are the ones that must. Both were destroyed
afterwards and the VM's count returned to the 12 it started with. Not validated live: the
refusal paths, which are unit tests.

### 2026-10-29 — v0.3 phase 4: the crash record, read from the event log

This is the one operation in the package that is not WMI: Hyper-V's own record of a guest
crash lives in the `Microsoft-Windows-Hyper-V-Worker-Admin` channel, and pywin32's event-log
API reads it without a PowerShell round trip and without parsing translated text.

- **Event 18590, seven named fields.** The template on this host was read off the provider
  (`Get-WinEvent -ListProvider 'Microsoft-Windows-Hyper-V-Worker'`, 786 events) rather than
  assumed: `VmName`, `VmId`, `VmErrorCode0` … `VmErrorCode4`, all `win:HexInt64`. The bridge
  reads the same seven by *position*; this reads them by *name*, which makes a reordered
  template a non-event.
- **The channel renders these events as `<UserData>`, not `<EventData>`.** That was the one
  surprise: `Get-WinEvent -ListProvider` shows a `<data name="VmName"/>` template for 18500
  as well, yet the event as the channel stores it is
  `<UserData><VmlEventLog><VmName>NORTH-Test</VmName>…`. A reader that only knew
  `<Data Name="...">` would have answered an empty payload for every real event on this host.
  Both renderings are read; the real 18500 XML is a fixture in the tests.
- **The window is XPath, not `Get-Date`.** `TimeCreated[timediff(@SystemTime) <= N]` is what
  `Get-WinEvent -FilterHashtable -StartTime` becomes, and it keeps the query free of a
  formatted local date.
- **A host without the channel answers no events**, as the bridge does — the guest-side
  event 1001 fallback still runs.
- **The stamp keeps all seven fractional digits.** `ToString('o')` prints .NET's 100 ns unit
  as the seventh; `datetime` stops at six, so `timefmt.local_iso` grew a `tick` argument and
  the digit is carried through instead of being padded with a zero.
- **The VM is matched by `ElementName`.** On the CIM side `Name` is the VM's GUID and the
  event carries the friendly name; `find_vm` matches the friendly one and the event is
  filtered against it.

Live validation (elevated, `NORTH-Test`): the WMI and the bridge answers for
`host_crash_events` were compared as whole dictionaries over a 720-hour window and were
**equal** — both empty, because this host has no 18590 in 90 days. The reader itself was then
run over real events of the same channel (id 18500, the id swapped in the probe): five events
came back with their `record_id`, their local `time` with seven fractional digits and the
`-03:00` offset, and the VM's `vm_name` and `vm_id` — so the query, the rendering and the
parser are exercised against real XML. **Not validated live: a real bugcheck.** No guest has
crashed on this host in the window, so the code and parameter fields have only unit tests;
producing one would mean crashing `NORTH-Test`, which is a deliberate operator decision.

### 2026-10-29 — v0.3 phase 4: a shutdown and a save that outlive the call

These are the two power actions a single call cannot finish, so they were the last two on the
bridge. They are now started from WMI and reported by `operation_status`, which is the same
two-call contract the bridge has.

- **A graceful shutdown is the shutdown component, not a state change.** `RequestStateChange`
  carries 4 ("Shut Down") in its `ValueMap`, but the method's documentation says that value is
  valid in Hyper-V V1 only. The V2 path is
  `Msvm_ShutdownComponent.InitiateShutdown(Force, Reason)`, found by `SystemName` — the VM's
  own id — because the component's `Name` is localized ("Desligamento" on this host).
- **Saving state is `RequestedState` 6, not the value the documentation names.** The page
  calls 32773 "Saving"; this host refuses it with 32775, and so it refuses 32774, 32776, 32777,
  32778 and 32780. Fast save (32779) is refused with a localized "não há suporte para o
  pedido" (0x80070032). **6 — CIM's "Offline" — is what saves the VM**, and the `EnabledState`
  afterwards is 6 (Saved) as well. Probed value by value on the live host.
- **The status is read when it is asked for, not remembered.** The bridge keeps a PowerShell
  job; here the operation keeps the object that carries it — the provider's job for a save,
  the VM's id for a shutdown — and `operation_status` reads it at that moment. No watcher
  thread, so nothing runs while the server is idle.
- **A settled operation stays settled.** Once the VM has reached the target the answer is
  latched, so starting the VM again afterwards does not turn a completed shutdown back into a
  running one.
- **A shutdown that never arrives is a failure, not a hang.** There is no job to fail, so the
  entry carries a 1800 s deadline; past it the status is `failed` with the state the VM was
  found in, which is what a guest ignoring the request would otherwise hide.
- **The registry is bounded at 50**, dropping finished entries before live ones, as the bridge
  does.

Live validation (elevated, `NORTH-Test`, running): `save` through WMI and through the bridge
were compared as whole dictionaries — the `vm_power` answers have the **same keys** and the
`operation_status` answers are **identical**; both ended `completed`/`succeeded: true` with the
VM `Saved`. `shutdown` through WMI ended `completed` with the VM `Off`. The VM was started
again after each and left Running. **One difference, deliberate:** the bridge's `vm` snapshot
in its `vm_power` answer reports the state *after* the transition started (`Saved`, in the run
above) because it converts the object once the job is running; this reports the state the call
found (`Running`), which is deterministic instead of a race.

Not validated live: the deadline path and the "VM is not Running" refusal, which are unit
tests.

### 2026-10-29 — v0.3 phase 4: the guest session, proved against a real guest

The persistent `New-PSSession -VMName`, the `Copy-Item -ToSession`/`-FromSession` transfer, the
uptime-based reboot detector and the second worker were all written in v0.2 and all had the same
caveat attached: *not validated live*. They now are, on `NORTH-Test`, through the same service the
MCP server builds (`GuestSplitBackend` + `Settings.from_env()`).

- **The session is reused, and it is the same runspace.** A first `guest_exec` sets a global
  variable in the guest; a second one reads it back. The answer was `REUSED:alive` — not "new
  session" — which no per-request `Invoke-Command` could produce, since each of those starts a
  fresh runspace. The first call took 0.66 s (it opens the session) and the second 0.03 s.
- **`Copy-Item` carries the file both ways.** 102 400 bytes out and back over the session, with
  the SHA-256 matching the host's in both directions.
- **A reboot is detected without an extra remote call.** `shutdown /r /t 3` in the guest took it
  down 16 s later and it answered again at 44 s; the next `guest_exec` reported
  `NEW SESSION` — the pre-reboot runspace was gone — and the call after that answered in 0.05 s
  from a freshly opened session. The uptime comparison in `Get-GuestSession` is what dropped the
  dead entry.
- **The second worker holds.** An `Start-Sleep -Seconds 8` guest command ran while `vm_get`
  answered in 1.0 s, so guest work no longer queues behind host work.

**What a reboot looks like from the caller, and it is not pretty.** Every call that lands while
the guest is restarting fails with the bridge's own timeout — *"The local bridge timed out. A
guest command may still be running; inspect the guest before retrying."* — because the request
is sitting inside `New-PSSession` for longer than the bridge's request timeout. That message is
honest but misleading for this case: nothing is running, the guest is simply not there yet. A
`guest_unreachable` classification for a call that fails during a reboot is the next thing this
area needs; until then, a caller retrying through a reboot sees `timeout` and has to know why.

Not validated live: two workers each holding a session to the *same* VM at the same time (the
overlap probe above proves non-blocking, not that both sessions coexist), a VM rebooted while a
transfer is in flight, and the session being closed when the worker's stdin loop ends.

### 2026-10-29 — v0.3 phase 4: the guest reports, the host decides

Publishing a staged package was the one place where the guest was asked for a *verdict*, and it
was written twice — once in the bridge for PowerShell Direct, once in `guest_operations.ps1` for
SSH — each with its own canonical-JSON assumption, its own path rules and its own hash
comparison. Two PowerShell implementations of the same decision drift, and when they disagree
the caller gets a bare "failed verification" from inside the VM with nothing to act on. That is
the class the roadmap's "minimal guest PowerShell" paragraph names.

- **The guest now returns facts.** `guest_package_inspect` answers the manifest bytes (base64),
  whether the package directory already exists, and a listing of every file under the staging
  directory with its size and `Get-FileHash`. It parses nothing, compares nothing, and has no
  opinion about paths. `guest_package_commit` does one directory rename and refuses an existing
  package. Both are the same two scriptblocks in both transports.
- **`lab/publish.py` makes the decision**, as a pure function: hash the bytes the guest sent,
  compare with the approved digest, parse, validate the manifest and every path, then match the
  listing entry by entry — a missing file, a changed size, a changed hash and an unlisted
  leftover each get their own message. No VM is involved, so the whole verdict is unit-tested
  (29 cases, `tests/test_lab_publish.py`) instead of being asserted through a fake guest.
- **`service.guest_publish_package` is the sequence**: inspect, decide, commit. The window
  between the two calls is one rename in a directory only the operator and this server write to,
  and the commit still refuses a package that already exists.
- **A latent bug fell out of this.** The SSH guest script returned `{path, files}` where the
  bridge returned `{guest_path, files_verified}`, so `lab_stage` over SSH would have raised
  `KeyError` on the answer. The service now builds the package answer itself from the commit's
  `guest_path` and the host's own count, so both transports answer identically.

Live validation (elevated, `NORTH-Test`, PowerShell Direct): `tests/live_lab_publish.py` staged a
two-file package, published it (`files_verified: 2`, `C:\DriverLab\Packages\live.c045ae77`),
re-verified it in the guest, and then had a staging directory with an extra `extra.dll` refused
by the host — *"The staged package contains files that are not in the manifest: extra.dll"* —
with nothing published. Not validated live: the same path over SSH, and a package whose manifest
bytes differ from the approved digest (unit test only).

### 2026-10-29 — v0.3 phase 4: `lab_verify` asks, and the guest stops canonicalizing JSON

The same split as publishing, applied to the check `lab_run` makes before it executes anything.
`lab_verify` used to shell out to `Test-DriverLabPackage.ps1`, which parsed the manifest, rebuilt it
as canonical JSON and hashed that — a second canonicalizer, in another language, whose only job was
to agree with `json.dumps`. When it disagreed, the caller learned nothing beyond a failed exit code.

- **`guest_package_list` is the new operation**, in both transports: the guest reports the directory
  path, whether it exists, the manifest bytes and a listing of every file with its size and hash.
  It shares one guest-side scriptblock (`$script:LabDirectoryFacts`) with `guest_package_inspect`,
  so the two answers cannot drift. It reports facts only.
- **`lab/publish.py` decides**, through the same `verify_staged` the publish path uses, with a
  `label` so a refusal says "published" rather than "staged". `DriverLabTransfer.verify` now returns
  `{verified: true, files_verified, manifest_sha256}` or `{verified: false, reason}`; a mismatch is
  a result, not an exception, because `lab_run` maps it to the `not_verified` outcome.
- **The guest's canonical JSON is gone.** `ConvertTo-LabJsonString`, `ConvertTo-LabCanonicalJson`
  and `Get-LabManifestHash` are deleted from `DriverLab.Common.psm1` — about 60 lines whose entire
  purpose was to reproduce Python's serializer byte for byte. `Test-DriverLabPackage.ps1` now hashes
  the manifest *file*, and keeps its per-file and unlisted-file checks as the tripwire the manual
  runner calls immediately before executing `Run-Lab.ps1`. A re-indented manifest that parses to the
  same document is now refused, which is the rule the host has always applied; a regression test
  (`test_package_verification_hashes_the_manifest_file_not_a_reserialization`) covers it.

Tests: 626 passing, 1 skipped. New: verification asks the guest for a listing and runs no guest
script; a swapped file is named in the refusal; the re-indented manifest is refused.

**Live validation on `NORTH-Test`, and the two defects it exposed.** `tests/live_lab_publish.py`
now also tampers with a published file and re-verifies. Over PowerShell Direct it staged and
published `live.a9d908ba`, verified it (`files_verified: 2`), then rewrote `driver.sys` in the
guest and got `verified: false, reason: "The published package failed verification: driver.sys"`.
Running the same flow over SSH found that **structured guest operations had never actually run
over SSH**, for two reasons that only appear on a real guest:

- **`guest_operations.ps1` was never uploaded.** `guest_ssh_provision` shipped only the setup
  script; the operations script was described as installed by provisioning and referenced by
  absolute path, so the first `guest_write_file` died with the remote shell's "O termo
  'C:\ProgramData\HyperVConsoleMCP\guest_operations.ps1' não é reconhecido como nome de cmdlet".
  Provisioning now uploads it (`_upload_operations`), and `guest_ssh_status` reports
  `operations_script: {path, present, sha256, matches, expected_sha256}` so a stale or missing copy
  is visible instead of surfacing as a confusing shell error.
- **The guest's execution policy blocked the script.** A guest left on `Restricted` refuses to load
  a `.ps1` by path, even from an encoded command, so the next attempt failed with "a execução de
  scripts foi desabilitada neste sistema". `SshTransport.encoded_line` now starts each call with
  `-ExecutionPolicy Bypass` for that one process; the guest's own policy is never changed.

Why it went unnoticed: the earlier live SSH run exercised `guest_exec` and `guest_status`, both of
which build an inline script and never load the operations file. After the fixes the same flow
passed over SSH (`transport: "ssh"`, `files_verified: 2`, then `driver.sys` named as the tampered
file), and SSH was deprovisioned afterwards.

Not validated live: the manual runner's tripwire (it needs an elevated guest console and a real
package to execute). Tests: 629 passing, 1 skipped.

### 2026-10-29 — v0.3 phase 4: the live config view is computed on the host

`guest_config.ps1` decided what its own readings meant: `($value -eq 1)` for every toggle,
`-not ($cad.value -eq 1)` for Ctrl+Alt+Del, a `switch` mapping `CrashDumpEnabled` to a mode name,
and the pagefile-versus-RAM comparison for a complete dump. That is a second place the same
reading exists, testable only on a VM.

- **`Get-LiveConfig` now reports facts**: the raw registry values, the raw BCD tokens, the power
  values, the interactive sessions and `dump_capacity {memory_bytes, pagefile_bytes}`.
  `Convert-DumpMode` and the comparison inside `Get-DumpSupport` (now `Get-DumpCapacity`) are gone.
- **`transport/guest_config.py::derive_live`** turns those facts into the shape the tools have
  always returned — `lock_screen_disabled`, `ctrl_alt_del_required`, `windows_update_disabled`,
  `driver_ready {…, crash_dump_mode, dump_support}` — so nothing downstream changed. It is applied
  in `_run`, which every mode (`Status`, `Apply`, `Revert`) goes through, so no mode can miss it.
- Two rules are stated instead of implied: a registry DWORD of `1` counts only when it is an
  integer (a string `"1"` or a `True` is not one), and an absent value stays `null` except where
  Windows itself defines the default — no `DisableCAD` means Ctrl+Alt+Del is required, no
  `CrashDumpEnabled` means no dump is configured.

Live, read-only on `NORTH-Test`: `guest_config_status` returned `crash_dump_enabled: 7` →
`crash_dump_mode: "automatic"`, `kernel_debug: true`, `test_signing: false`, and
`dump_support {memory 4 293 791 744, pagefile 1 476 395 008, sufficient: false}` — the derived view
matching the raw facts the guest sent. Tests: 632 passing, 1 skipped.

### 2026-10-29 — v0.3 phase 4: the lab diagnostics report tokens, not verdicts

`Get-DriverLabDiagnostics.ps1` was the last guest script that concluded instead of reporting: it
ran `[bool]($boot -match 'testsigning\s+Yes')` and `'debug\s+Yes'` over the bcdedit dump and shipped
`test_signing`/`debug_enabled` as facts. That made the only reading of those tokens a regex inside a
script that can be run only on a VM, and it was a second bcdedit parser next to the one in
`guest_config.ps1`.

- **`bcd.py`** now holds the one reading of a BCD token: `Yes`/`on`/`true` are on, `No`/`off`/
  `false` are off, an absent element is off (bcdedit omits a setting that is disabled), and a token
  this build does not recognise is `None` rather than a wrong `False`. `derive_live` uses it, so the
  guest configuration view no longer depends on a normalisation written in PowerShell either.
- **`Get-LabBcdToken`** in `DriverLab.Common.psm1` returns the token exactly as bcdedit printed it.
  `guest_config.ps1` still normalises to on/off, but only for the value it writes back on revert;
  its live view reports the raw token too (`boot.testsigning`, `boot.debug`).
- **`lab/diagnostics.py`** derives `test_signing` and `debug_enabled` from `boot_tokens` and keeps
  the raw facts beside them. `lab.cli.diagnostics` adds that as a `diagnostics` key and leaves the
  raw `execution` untouched, so a report the host cannot parse (a failed run, a warning line) is
  returned exactly as it arrived.

The public shape of `lab_diagnostics` is additive: the derived answers moved from inside the
guest's JSON text to a parsed object the tool returns.

Live on the pt-BR `NORTH-Test`, read-only after `lab_init` installed the two updated tools:
`boot_tokens {debug: "Yes", testsigning: null}` — bcdedit printed its labels in Portuguese
("Configurações do Depurador") and the element names untouched — derived to `debug_enabled: true`
and `test_signing: false`, which `guest_config_status` reported independently as
`kernel_debug: true`, `test_signing: false`. Tests: 640 passing, 1 skipped.

### 2026-10-29 — v0.3 phase 1 sweep: an access denial is read from the exception, not the message

`Invoke-BridgeRequest` decided `access_denied` from the top-level HResult and, as a fallback, from
`$message -match 'permission|permiss|access denied|acesso negado'` — a list of English and
Portuguese words. A Hyper-V cmdlet that wraps the denial in a generic exception therefore reported
`bridge_error` on a host in any other display language, and the caller saw the raw cmdlet error
instead of "run elevated or grant Hyper-V Administrators access".

`Test-AccessDeniedError` walks the inner-exception chain for `UnauthorizedAccessException` or
`E_ACCESSDENIED`, the same way `Test-NotFoundError` already walks it for file-not-found, and it runs
before every other classification. The word list stays as the last resort for a CIM error that
carries the denial only in its text, and is now marked as the heuristic it is.

Test: a wrapped denial carrying a German message, which came back as `bridge_error` before the
change. Tests: 641 passing, 1 skipped.

### 2026-10-29 — v0.3 phase 5: the guest describes itself without a credential

The first piece of zero-config discovery: `vm_get` and `vm_list` now carry `guest_facts` for a
running VM — the computer name, OS name, version and build, and the integration services version the
guest publishes through Hyper-V's key-value pair exchange. One WMI query, no password, so a tool can
name the guest before any credential exists.

- **`hostwmi/kvp.py`** reads `Msvm_KvpExchangeComponent`, scoped by `SystemName`. The component's
  `DeviceID` is the device's own GUID, not the VM's, so matching on it silently found nothing on the
  first attempt — confirmed against the live host before wiring it in.
- Two limits are stated rather than papered over. `NetworkAddressIPv4`/`IPv6` and
  `RDPAddressIPv4`/`IPv6` are deprecated and now carry a deprecation *notice* where an address used
  to be, so they are filtered out; guest addresses come from
  `Msvm_GuestNetworkAdapterConfiguration`, which the adapter mapping already reads. And a stopped VM
  reports `guest_facts: null` rather than an exchange with no items, because there is no guest to
  describe — the query is also skipped for it, so a listing of stopped VMs costs no extra call.
- A missing exchange component, a stopped Data Exchange service and a failed query each come back as
  `available: false` with the reason, never as an exception: these facts enrich a listing, and a VM
  whose integration service is missing must still be listed.

Live on `NORTH-Test` through the WMI backend: `guest_facts.computer_name` `DESKTOP-AA9PSTJ`,
`os_name` `Windows 10 Pro`, `os_version` `10.0.19045`, and `vm_list` reporting `available: true` for
the same VM. This guest publishes no `ComputerName`, only `FullyQualifiedDomainName`, which is why
the convenience field falls back to it. Tests: 647 passing, 1 skipped.

### 2026-10-29 — v0.3 phase 5: the dashboard names no VM of its own

Two constants were baked into the control center: `--vm` defaulted to `NORTH-Test` and the debugger
page's COM1 pipe defaulted to `north-kd`. On any other host the first is a guess that either fails or
silently attaches the window to the wrong VM, and the second means two VMs configured from one
installation collide on a single pipe.

- **`--vm` is optional.** `dashboard.initial_vm_name` decides what opens without asking: an explicit
  name wins; otherwise the candidates are the VMs Hyper-V reports, narrowed by
  `HYPERV_MCP_ALLOWED_VMS` when it is set. One candidate opens directly — a zero-config host with a
  single VM needs no argument and no allowlist — and two or more open the `_VmChooser` list instead of
  picking one. The chooser also accepts a typed name, so a VM the discovery call could not see is
  still reachable, and cancelling exits without starting the event reader or the approval broker.
- **The pipe name is derived from the VM.** `hostwmi/serial.py::suggested_pipe_name` turns the name
  into what the provider's own regex accepts (`<slug>-kd`, at most 64 characters, lowercase, runs of
  rejected characters collapsed to one hyphen), and the value is now created once per window instead
  of being rebuilt on every visit to the Debugger page, which had been discarding an edited name.
- A regression test reads every module under `src/hyperv_console_mcp` and fails if `NORTH-Test` or
  `north-kd` appears again, so the defaults cannot creep back in. Documentation and fixtures keep
  their examples.

Not validated live in this slice: the chooser itself needs an interactive desktop, so it is covered
only by the pure selection rule (`initial_vm_name`) and by the existing headless render test that
constructs the window with an explicit VM. What was checked live is the discovery rule: elevated on
this host, `list_vms` reported `NORTH-Test` as the only VM and `initial_vm_name` returned it both with
and without `HYPERV_MCP_ALLOWED_VMS` set, so the window opens with no argument at all. Tests: 659
passing, 1 skipped.

### 2026-10-29 — v0.3 phase 5: one level per VM instead of seven flags

The allowlist and the seven `HYPERV_MCP_ALLOW_*` variables said the same thing twice — once per VM,
once per capability — and every capability was host-wide: there was no way to let the AI drive one
VM and only read another. `access.py` names the same policy in one word per VM.

- **`read-only`** lists the VM and reads its state and nothing more; **`control`** adds power,
  checkpoints, console input, screenshots and media; **`lab`** adds guest access and the debugger.
  Guest access and the debugger are what separate the last two, because both put code or a debugger
  inside the guest — the line this project keeps everywhere else.
- **The file is the policy and the allowlist.** `%LOCALAPPDATA%\HyperVConsoleMCP\access.json` (or
  `HYPERV_MCP_ACCESS_FILE`) maps VM to level, written by `hyperv-access set|list|remove` — run by the
  operator on the host. No MCP tool writes it, and no tool can add a VM: a policy the AI could widen
  is not a policy. The write goes to a temporary file and is renamed into place, so a reader never
  sees half a policy; a file that exists but cannot be parsed stops startup rather than degrading to
  "no VMs allowed", which would look like a broken host and hide the typo.
- **Precedence, stated once.** A VM with a level takes its capabilities from that level; a VM without
  one keeps the old behaviour, where the profile and the environment flags decide for every VM at
  once. Per VM is more specific, so it wins. `HYPERV_MCP_ALLOWED_VMS` narrows the file rather than
  replacing it, which is what keeps every existing test and one-off run meaning what it meant.
- `_write(name, enabled, variable)` became `_write(name, capability)`, and 27 call sites became
  `self._write(vm_name, "power")` and so on; `Settings.refusal` now words the error in whatever
  decided it (the VM's level, or the variable to set). `host_status` reports the level map beside the
  global flags, and the dashboard's sidebar shows the chosen VM's capabilities rather than the host's.

Tests: `tests/test_access.py` — the level-to-capability mapping, a read-only VM refusing a mutation
it would otherwise be allowed, `control` reaching power but not the guest, a level overriding a
host-wide flag, the file supplying the allowlist and the environment narrowing it, a malformed file
and an unknown level both refused, and the round trip through the file with no `.tmp` left behind.
The `hyperv-access` CLI was exercised end to end against a temporary file; the real access file on
this host was read (empty) and deliberately not written — granting `lab` to a VM is the operator's
decision, not this loop's. Suite: 674 passing, 1 skipped.

### 2026-10-29 — v0.3 phase 5: a refusal carries the call that would fix it

- **The contract.** `HyperVError` now carries an optional `fix`, and only two shapes are allowed:
  `{"tool": <registered name>, "args": {...}}` when the caller can close the gap itself, and
  `{"operator": <what a human must do>}` when it cannot. `server._failure` renders it as one extra
  line of JSON (`code: message\nfix: {...}`) so a client parses it instead of reading prose. The
  same `fix` travels through `WmiFailure` → `BackendError` → `ToolError`, which is the path that
  would otherwise drop it silently.
- **Three producers so far.** A missing guest credential asks the operator to store one (and never
  mentions a secret); an SSH connection that refuses, or a guest with no pinned key, offers
  `guest_ssh_provision`; `debug_serial_prepare` on a running VM offers `vm_power` with `shutdown`,
  because the provider will not change a port on a live VM and the server will not stop it.
- **Refusals from the policy layer name the narrowest level that would work.** `_raise_to` reads
  `minimum_level(capability)`, so a `control` VM refused the guest is told `lab`, not `lab|control`;
  a VM with no level at all is offered the level *and* the older `HYPERV_MCP_ALLOW_*` variable,
  because both answers exist for it. An unlisted VM is offered the command that lists it.

Tests: `tests/test_fix_contract.py` — the two renderings, `_invoke` turning a fix into a `ToolError`
line, a source sweep that every fix naming a tool names a registered one, a second sweep that the
arguments match the tool's schema (required keys present, no unknown keys, literals inside their
enum), the missing credential carrying an `operator` fix and no secret, and the level refusals
naming `lab`/`control` as appropriate. `tests/test_hostwmi_serial.py` and
`tests/test_ssh_transport.py` gained the producer-side assertions, including the WMI layer keeping
the fix it was handed. Suite: 689 passing, 1 skipped. Not exercised live: every producer above is
reached through a unit path, because each one needs a specific host or guest state (no credential,
no sshd, a running VM) that the live host was not put into for this change.

### 2026-10-29 — v0.3 phase 5: one service, one owner, one pipe

- **The boundary.** `service/remote.py` holds everything about crossing a process that does not
  need a pipe: a request names a public method and carries its arguments, a response carries either
  the encoded result or the error's code, message and fix. `encode`/`decode` round-trip the four
  types JSON would flatten (`Screenshot`, `bytes`, `Path`, `tuple`) and *refuse* anything else with
  a `TypeError` instead of stringifying it. `dispatch` refuses a private name rather than looking it
  up — `_call` and `_approve` would run an operation with the policy checks the caller was supposed
  to go through, and `backend` would hand the pipe a second door to the host — and binds arguments
  to the signature *before* running, so a wrong-shape call names the offending argument instead of
  failing halfway through a mutation.
- **The pipe.** `service/host.py` is the actual Windows channel, with the approval broker's
  hardening reused: a security descriptor for the current user's SID only,
  `FILE_FLAG_FIRST_PIPE_INSTANCE` on the first instance so a second host fails instead of quietly
  sharing the name, `PIPE_REJECT_REMOTE_CLIENTS`, and `_verify_server_process` before a client sends
  anything. Framing is a length prefix, because a screenshot is megabytes and a message-mode read
  that outgrows its buffer fails with `ERROR_MORE_DATA` — that was the first bug the pipe tests
  found. `SERVICE_LOCK` keeps one operation at a time inside the service, which is what its managers
  already assume. `hyperv-service` runs the host in the foreground; a client that finds no host is
  told to start one.
- **The service owns what cannot cross.** `capture_screenshot` no longer takes a capture object: a
  remote client cannot pass a live window handle over a pipe, and should not — the desktop being
  captured is the host's — so `ServiceBase` builds `VMConnectCapture` where the host runs.
  `event_snapshot` likewise: the operation bus is the owner's, and a client reading its own copy
  would get an empty snapshot the moment the service moved to another process.
- **`{code, fix}` survives the trip.** `error_from` rebuilds the exception class from the code, so a
  `PolicyError` crosses as a `PolicyError` and the fix arrives intact for the client to act on.

Tests: `tests/test_service_remote.py` (the codec's four types and its refusal, a screenshot
surviving, an error keeping its code and fix, private and unknown methods refused, arguments named
before the run, no traceback leaked, a source sweep that every method the server and dashboard call
exists on the service, and the envelope keeping keyword arguments and types);
`tests/test_service_pipe.py`, Windows-only (a call and a refusal across the pipe, a second host
refusing the name, a client with no host told how to start one, a 2 MB result crossing the buffer,
and four threads proving two callers never run inside the service at once). Suite: 716 passing,
1 skipped. Not exercised live: the pipe has only been driven by tests in this process, not yet by
an MCP server and a dashboard in two processes at once.

### 2026-10-29 — v0.3 phase 5: the server and the window are clients of one host

- **`ensure_running`.** A client that finds no host starts one detached
  (`python -m hyperv_console_mcp.service.host`, `DETACHED_PROCESS`, output to
  `%LOCALAPPDATA%\HyperVConsoleMCP\service\service.log`) and waits for the pipe, rather than
  running a second service inside itself: two owners would mean two workers, two serial sessions
  and a dashboard that cannot see what the AI opened. `HYPERV_MCP_SHARED_SERVICE=false` keeps the
  private copy for a test or a one-off run, and `HYPERV_MCP_SERVICE_PIPE` names another pipe.
- **The actor travels in the envelope.** The event bus and the approval gate read the actor from a
  context variable in the *service's* process, so a client now says who it is per call.
  `dispatch` sets it for the duration, which is what makes a dashboard call record itself as
  `dashboard` and not prompt itself for approval while an AI call still does.
- **The host publishes.** `hyperv-service` wraps its backend in `EventSink`, so bridge operations
  publish from the process that runs them; the window reads `service.event_snapshot()` instead of a
  local bus that would always be empty, and keeps the JSONL reader for history.
- **Known limitation, stated rather than hidden.** The host reads its policy once at startup, so
  whichever client starts it decides the environment for both — an MCP host launched with a
  read-only environment would make the window read-only too. The access file (one level per VM) is
  the answer the roadmap is moving toward, and the launcher will start the host with the operator's
  environment rather than a client's.

Tests: `tests/test_service_remote.py` gained the actor crossing the envelope (and a blank one
falling back to `mcp`), a dashboard call not asking itself for approval while an `mcp` call still
does, and the surface sweep now requires a call parenthesis so a module path (`service.host`) is not
mistaken for a method. `tests/test_service_pipe.py` gained `ensure_running` joining a host that is
already serving and refusing to spawn another. `tests/test_config.py` covers the
`HYPERV_MCP_SHARED_SERVICE` default, its explicit false, and a refusal for anything else.
`tests/test_dashboard.py` now runs with a private service, because the rendering test must not
reach the machine the suite runs on. Suite: 720 passing, 1 skipped.

Validated live with `tests/live_service_host.py --vm NORTH-Test`: a real `hyperv-service` on its own
pipe, a real MCP stdio client pointed at it through `HYPERV_MCP_SERVICE_PIPE`, `hyperv_host_status`
succeeding, `vm_list`/`vm_get` refused with `access_denied` because the shell was not elevated, and a
**second** client then reading the six operation events those three calls produced. That is the
defect this slice exists to fix, observed rather than inferred. Not exercised live: the dashboard
window itself against the shared host (it is still tkinter and needs a display), and the approval
gate across two processes with a real click.

### 2026-10-29 — v0.3 phase 5: the level is chosen in the window, not only in a terminal

- **The gap.** `hyperv-access` was the only writer of the access file, while the roadmap has the
  level chosen in the UI — and the shared service made the gap worse: a policy read once at
  startup would have needed a restart for a level set in a terminal to matter.
- **`reload_policy`.** The service re-reads the environment and the operator's file on request and
  returns the levels and the allowlist it now enforces. It cannot widen anything by itself — it
  only re-reads what the operator wrote — so it is a public service method and an AI call may ask
  for it too.
- **Both writers notify.** `hyperv-access set`/`remove` and the window's new *Access level* frame
  write the file and then call `reload_policy`; a host that is not running is reported as
  `service: not running`, not as a failure, because the next client starts one and it reads the
  file then. The frame says what a level means, and says plainly that a VM with none cannot be
  reached by any tool.

Tests: `tests/test_access.py` — a level written while the service runs is enforced after
`reload_policy`, and the CLI's notification degrades to `not running` with no host;
`tests/test_dashboard.py` covers the sentence the window shows with and without a level. Suite:
723 passing, 1 skipped.

Validated live: a real host on its own pipe reported `levels: {}`, `hyperv-access set NORTH-Test lab`
answered `service: reloaded`, and the same host then reported `{'north-test': 'lab'}` — the whole
loop, observed. Not exercised live: a click on the window's *Apply level* button (the frame needs a
display and an operator to confirm it).

### 2026-10-29 — v0.3 phase 5: an approval is a card, not a window that takes the desk

- **The gap.** The approval prompt was a `tk.Toplevel` with `grab_set()` and `wait_window()`: while an
  AI operation waited, the window could not be touched — exactly when the operator wants to read the
  VM's state, another page, or the previous run before deciding. Two requests at once were not
  representable at all.
- **A side queue.** The window is now a workspace with the page in one grid column and
  `_ApprovalQueue` in the other. Each request is a card with the operation, the VM, the tool's own
  secret-free summary and two buttons; the column appears when something is waiting and leaves when
  the last card is answered. Nothing grabs the window.
- **The card follows the broker.** A card is keyed by the broker's request id, and `_prune_approvals`
  drops any card whose id is no longer in `self.pending` — so a request that timed out on the
  broker's own clock cannot leave a button that looks live but answers nobody.

Tests: `tests/test_dashboard.py` — a request becomes a card while `grab_current()` stays `None`,
answering one of two queued requests answers only that one, and a stale card leaves the queue on the
next tick. Suite: 741 passing, 1 skipped.

Not exercised live: a real MCP mutation held open while the operator clicks *Approve once* (it needs
a running host, the approval environment and a human click). The queue itself was exercised in a real
window over the fake backend, which is where the widget code runs.

### 2026-10-29 — v0.3 phase 5: `hyperv-install` writes the client's file, not a template

- **What it does.** `hyperv-install --client claude|codex|cursor` merges one `mcpServers.hyperv`
  entry into that client's own configuration, `--uvx` writes `uvx hyperv-console-mcp` instead of the
  installing interpreter, `--profile` sets `HYPERV_MCP_PROFILE`, and `--shortcut` adds a Start Menu
  entry that opens the window through `pythonw.exe`.
- **Why it refuses rather than rewrites.** A file that is not valid JSON, or whose `mcpServers` is
  not an object, is an error and is left byte-identical. Codex's TOML gets one appended section —
  re-serialising it without a parser would reorder it and lose comments — and an existing
  `[mcp_servers.hyperv]` header means `unchanged`. Every file that is changed is copied first, and a
  second identical run reports `unchanged` rather than piling up backups.
- **Out of the MCP surface.** No tool reaches this module: an install step an AI could run would be a
  way to make itself a permanent client.

Tests: `tests/test_install.py` (15 tests) — per-client paths, the entry with and without `uvx`, create
/ merge / unchanged / refused JSON, TOML append and refusal to touch an existing section, the Start
Menu path, the non-Windows skip, and the CLI writing the client it was given. Suite: 741 passing,
1 skipped.

Validated live, in a throwaway profile. The package was installed into a temporary virtual environment,
so `hyperv-install.exe` was the real console script, and `USERPROFILE`/`APPDATA` pointed at a scratch
directory: `--client cursor --profile lab` created the file with the venv's own interpreter,
a second identical run reported `unchanged`, a file containing `{oops` was refused with exit 1 and
left byte-identical, `--client codex` created the TOML section, and `--shortcut` produced a real
`Hyper-V Console MCP.lnk` under the scratch Start Menu. Not exercised: writing into this host's own
Claude, Cursor or Codex configuration — that is the operator's file and their decision.

### 2026-10-29 — v0.3 phase 5: the window is a page, and it does not need a port

- **What replaced tkinter.** `webui/` holds `api.py` (what the page may ask), `window.py` (opening it)
  and `assets/index.html` (one self-contained dark page). The page is passed to pywebview as `html=`
  with every asset inline, so `webview.start(http_server=False)` starts no local server and the
  window opens no port — the same rule the MCP server keeps. `hyperv-dashboard` opens it and falls
  back to the tkinter window when pywebview or the WebView2 runtime is missing; `--classic` asks for
  tkinter on any host.
- **The bridge is not a second door.** `Api.call` sends `{method, actor, kwargs}` through the same
  `dispatch` the pipe uses, so only public service methods cross, arguments bind to the signature
  first, and a refusal arrives as `{error: {code, message, fix}}` for the page to render. The level
  file is the window's own (`hyperv-access`'s file, which no tool reaches), and approvals are cards
  in a side column, so the page never takes the desk while an operation waits.
- **A `const value` broke the whole page.** pywebview answers a JS → Python call by evaluating
  `var value; …` at global scope. A page that already declares `value` at top level turns that
  script into a `SyntaxError`, so the call never returns and the window draws with nothing behind
  it: no error, no traceback. The helper is now `fieldValue`, and a test scans the page's top-level
  declarations for the names pywebview's own wrapper uses.
- **Layout fixed against the real window.** The nav buttons were inline-blocks and paired two to a
  row; the narrow layout kept the column rule and pushed the brand out of place; the screenshot
  `<img>` drew a broken-image icon before any capture; an empty `<dd>` read as a failed load; and a
  VM-less window rendered `" · profile safe"` with a leading separator.

Tests: `tests/test_webui.py` (19 tests) — envelope dispatch, private-method and wrong-argument
refusals, `hello` and its capabilities, choosing a VM, writing and removing a level, the approval
queue answering once and denying on its own timeout, the screenshot data URL, the page carrying no
`http://`/`https://`/`<script src`/`<link` and all eight builders, the runtime probe, and the
name-collision regression. Suite: 762 passing, 1 skipped.

Validated live on this host. The window opened at 1420×900 and at 1000×760 over a fake service and
over the real one; all eight pages rendered; an approval request became a card with the operation,
the VM and the command preview, and *Approve once* answered `True` and removed it; the VM chooser
listed both VMs and switching to one at `control` greyed out guest and debugger in the capability
list. The real `hyperv-dashboard` opened against the shared service and read the host (VM state
`unknown` and `hypervisor: unreadable`, which is this shell's missing elevation, not the window).
Not exercised live: a real MCP mutation held open while the operator clicks, and a screenshot
rendered from a live VM — the capture path needs an elevated session.

- **A pipe name the shell ate, said plainly.** `HYPERV_MCP_SERVICE_PIPE` without its leading `\\`
  made `CreateNamedPipeW` fail with WinError 123 inside the *detached* host, so the client reported
  only "the service did not answer within 20 seconds" and the reason sat in a log file nobody opens.
  `pipe_name()` now refuses a malformed override where it is read, and `hyperv-dashboard` shows a
  service failure in a message box instead of exiting silently when launched from the shortcut.

### 2026-10-29 — v0.3 phase 6: Driver Verifier, recorded and revertible

- **State read from the registry, never from `verifier /query`.** `transport/guest_verifier.ps1` reads
  `HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\VerifyDriverLevel` and
  `VerifyDrivers`; the query output is translated on a pt-BR guest, which is the same class of defect
  phase 1 fixed for bcdedit and powercfg. `transport/guest_verifier.py` names the flags and reports
  undocumented bits as `0x…` numbers instead of guessing.
- **Record and revert, not "disable".** Enabling writes `verifier.json` under
  `C:\DriverLab\State\` holding what was there before; `Revert` restores that and removes the file,
  so an operator's own Verifier setting survives the lab. Driver names are validated as names, never
  as paths.
- **The read side lives in `lab_diagnostics`.** A second listing tool would have cost a schema in
  every conversation for one field; the diagnostics report already answers "what is this guest's
  test state".

### 2026-10-29 — v0.3 phase 6: a test driver's service lifecycle, with the numbers

- **advapi32, not `sc.exe`.** `transport/guest_driver_service.ps1` opens the SCM with
  `OpenSCManager`/`CreateService`/`StartService`/`ControlService`/`DeleteService`, so the Win32 error
  from `StartService` and the driver's own NTSTATUS (`SERVICE_STATUS.dwServiceSpecificExitCode` under
  `ERROR_SERVICE_SPECIFIC_ERROR`) come back as numbers rather than as translated text.
- **A NULL that PowerShell turned into a machine name.** `OpenSCManager` failed with Win32 123
  because a `[string]` P/Invoke parameter receives `""` for `$null`, and the SCM reads `""` as a
  machine name. The sentinel has to be
  `[System.Management.Automation.Language.NullString]::Value` *inline*: assigning it to a variable
  turns it back into an empty string. Both call sites carry that comment.
- **Signature enforcement is never bypassed.** The image must already be a `.sys` inside
  `C:\DriverLab`; a refused load is reported with its code and the approval record says
  `bypasses_signature_enforcement: False`.

### 2026-10-29 — v0.3 phase 6: a crash is triaged from the dump the run already collected

- **The newest minidump, not `MEMORY.DMP`.** `lab/crash.py::choose_dump` prefers a file under
  `minidumps/` and breaks ties by the `modified_utc` the guest now stamps into `evidence.json`;
  `!analyze -v` on a kernel dump reads as much as the guest has RAM. An index entry that escapes the
  run directory is ignored, and a dump the host does not have is reported rather than opened.
- **kd's reading is merged with the host's, not substituted for it.** `merge()` keeps the host's
  event-18590 code and parameters when kd cannot open the dump or is not installed, and lists which
  sources answered. A run that had a debugger attached and got an answer is not analyzed twice.
- **The log is kept.** The full `!analyze -v` output is written beside the dump as `kd-analyze.log`,
  and a triage failure never costs the run its report.

### 2026-10-29 — v0.3 phase 6: whether the driver's symbols actually resolved

- **`!lmi` first, `lm` as the fallback.** `kd/symbols.py` parses both; a PDB that kd found and then
  rejected (`*** Mismatched PDB ***`) reads as unresolved, not as a load, because the stack has no
  names either way. The lab flow asks for every `.sys` the package ships once kd is broken in, and
  reports the resolved and unresolved sets beside the analysis.
- **The dump path gets the same verdict without a second kd run.** An analysis that names a function
  resolved symbols; one that names a module and an offset did not. `crash.merge` carries
  `symbols_resolved` into the run report.
- **The symbol path is in the approval summary.** `kd_attach` shows the `srv*` entry and every lab
  package directory that carries a PDB before the session starts.

### 2026-10-29 — v0.3 phase 6: every cycle writes down what it did

- **`lab-cycle.json` beside the evidence.** `lab/report.py` keeps the package and the manifest hash
  the run was asked to match, the checkpoint and its revert, the outcome, the crash reading with
  `symbols_resolved`, the debugger's symbol result, and the evidence as one hash over its files —
  sorted and indented, so two runs of the same package are a file diff.
- **It is written last, and never fails the cycle.** A host that cannot write it still has the run
  report: the record is a copy of the answer, not the answer.

### 2026-10-29 — v0.3 phase 7: the secrets scan is proved able to fail

- **A build that leaks must turn the scan red.** `LeakingService` returns a KDNET preparation whose
  result carries the password, and the same `assert_clean` the real scenario uses is expected to
  raise against it. A scanner that stays green on a leak cannot fail on anything.
- **One planted leak per scanner.** A file under the evidence root, a JSONL event and an audit line,
  each carrying a secret, so `assert_tree_clean`, `assert_events_clean` and `assert_audit_clean` are
  each shown to read what they claim to read rather than passing because the directory was empty.
- **The fixture no longer reads the operator's own event directory.** `Settings(...)` built directly
  takes the field default, not `HYPERV_MCP_EVENT_DIRECTORY`, so the scan was reading
  `%LOCALAPPDATA%\HyperVConsoleMCP\events` — and the negative control wrote a file there. The
  fixture now passes `event_directory` explicitly, which also makes the events assertion mean
  something on a machine that has never run the server.

### 2026-10-29 — v0.3 phase 7: the smoke counts the surface per toolset

- **`hyperv_host_status` reports `toolsets.counts` and `total`.** The counts come from the
  registration wrappers, so the report cannot disagree with what was registered, and the README's
  tool table stops being the only claim about it.
- **The smoke starts three surfaces, not two.** `safe` (29), the `lab` profile (42) and every toolset
  (55), each asserted per family — eleven server starts were not needed to check eleven families. The
  `lab` run keeps the explicit `HYPERV_MCP_ALLOW_*=false` values, which still win over the profile,
  so the denied mutation stays a policy refusal rather than a real power operation.
- **The README said `lab` was 43.** It registers 42; the count is now asserted in
  `tests/test_server.py` and in the smoke instead of only being written down.

### 2026-10-29 — v0.3 phase 7: CI runs the suite on Linux too

- **The `import-check` job became a `linux` job.** It keeps the explicit import of `capture`, `ipc`
  and `serial` — the modules whose `ctypes.WinDLL` calls are all inside functions — and adds ruff and
  the whole unit suite, which skips the Windows-only tests by their own `skipif`.
- **`v0.3` pushes build.** The workflow listed `main` and the v0.2 branch only.

### 2026-10-29 — v0.3 phase 7: the docs catch up with phase 6

- **`docs/DRIVER-TESTING.md`** is the phase 6 flow in order: prepare the guest, build the package,
  turn Driver Verifier on, run the cycle, read the crash and the symbol verdict, diff two
  `lab-cycle.json` records. It also says what the flow does not do, including why the IOCTL harness
  is the operator's `Run-Lab.ps1` rather than a tool.
- **`SECURITY.md` gains the single-service model** — one pipe with `FILE_FLAG_FIRST_PIPE_INSTANCE`,
  the policy read once by whichever client starts the host, and the fail-closed broker — plus the
  Verifier, driver-service and crash-artifact surface, and the negative control the secrets test now
  carries.
- **The window's Lab page no longer says a payload stays manual.** In the lab profile it offers Run
  and Cycle with the manifest hash the run must match; outside it, it states the manual boundary and
  the phrase the guest runner asks for.

### 2026-10-29 — the v0.2 Definition of Done, audited

- **`tests/test_transport_conformance.py`** runs one assertion set over `psdirect` and `ssh`:
  the result keys, the transport named in every result, truncation at the configured limit, a
  failing command reported as a failed result, a put/get round trip whose digests match, and a
  structured operation refused before the guest is touched. Each transport had its own fakes
  and its own assertions; nothing checked that they agree, which is the property the lab runs on.
- It was proved able to fail: removing the SSH truncation fails the `ssh` case and leaves
  `psdirect` green.
- **The roadmap's Definition of Done is now a table** at the end of this file, with each box
  pointing at the artefact that checks it. Three boxes stay open — KDNET live, `kd_dump_open`
  against a real dump, and bugcheck-versus-hang with no debugger — because each needs a guest
  reboot or a payload, and running a payload is the operator's separate decision.

### 2026-10-29 — dashboard wave 1: the fleet, a live feed, and workspaces

- **The page no longer polls for state the service can push.** `events_wait` is a service
  method now: it holds the caller on the event bus's own condition until something is
  published or the timeout expires, so the window is told when there is news instead of
  asking every 800 ms. The hold is clamped to 30 seconds, because a page that asks for an
  hour would park a thread for an hour.
- **The live feed does not take the worker lock.** `events_wait` answers from the bus alone,
  and the host now dispatches `event_snapshot`/`events_wait` through `dispatch_request`
  without `SERVICE_LOCK`. Taking it would stop every other client for the length of the wait,
  which is worse than no live feed.
- **`Convert-Vm` reports `cpu_usage_percent`** — the percentage Hyper-V itself sampled at
  `Get-VM`, shown as-is rather than computed from a second read.
- **The window has a fleet page and workspace tabs.** Every VM Hyper-V reported is listed with
  its state, its own level badge, CPU, memory and its power actions; opening one adds a tab
  and the per-VM pages appear. Closing the last tab clears the VM (`choose("")`), so the page
  falls back to the fleet instead of keeping a VM the operator just closed.
- **A connection indicator** says whether the last `events_wait` reached the service; a failed
  one retries after two seconds rather than drawing a feed that stopped updating silently.
- Tests: the wait returns at once when nothing happened and returns what was published while
  it waited; a non-sequence cursor is refused; the 30 s clamp and the zero floor are asserted
  against the bus; `dispatch_request` enters the lock for `list_vms` and never for
  `events_wait`; `hello` carries `levels_by_vm`; the page's `events` call crosses the envelope.
- Two tests were wrong about the window rather than about the code: the approval-pruning test
  showed a card whose id the broker had never registered (the 100 ms pruner could drop it
  before the assertion), and the chooser test still expected an empty name to be an error.
- Still open: waves 2–5 (interactive console, checkpoints, file manager, lab/debugger, matrix),
  the `docs/DASHBOARD.md` rewrite, and the console cadence measurements for
  `docs/PERFORMANCE.md`.

### 2026-10-29 — dashboard wave 2: a console you can click, and a baseline the page cannot move

- **The baseline became service state, not a page rule.** It is written to the operator's
  access file beside the level (`AccessStore.set_baseline`, `Settings.baseline_for`) and
  enforced in `VmService`: `delete_checkpoint` refuses the baseline for every actor, and
  `create_checkpoint` refuses to reuse its name, so a checkpoint called "golden" cannot be
  quietly replaced. The window only reads it — the rule lives where the AI also meets it.
- **`list_checkpoints` marks the baseline** on the checkpoint it names and repeats the name
  at the top level, so the tree is drawn from the service's answer rather than from a guess
  in JavaScript.
- **The console is an image you can click.** `console_click`/`console_mouse_move` receive
  guest pixels, so the page maps the `<img>`'s CSS box back through `source_width`/
  `source_height` from the screenshot metadata; `capture_scale` and `coordinates_match_mouse`
  decide whether the mapping is honest, and the page says so instead of guessing. Typing is
  batched into one `console_batch` and keys go through a name table; a "Ctrl+Alt+Del" button
  and a paste box cover OOBE, where the guest has no clipboard to share.
- **Capture stops for a reason.** The loop pauses when the window is hidden or the operator
  pressed pause, and it *stops asking* — rather than retrying twice a second — when the
  service refuses, which is what an Off VM or a `control`-less level looks like. A refused
  frame is a message on the page, not a spin.
- **Checkpoints: create, restore, delete, with the baseline locked.** Restore and delete
  confirm in place (two clicks on the same row, no modal), and the page draws branches from
  `parent_id` rather than assuming a flat list.
- **`OPERATOR_ACTORS` stops the window asking itself for permission.** `ui` and `dashboard`
  are the operator deciding; a card for a click the operator just made would be a second
  question about the same decision. The AI (`mcp`) is still gated exactly as before.
- Tests: the baseline survives a level change and is stored in the file; it needs a level to
  belong to; a file holding a bare level still loads; `baseline_for` reaches `Settings`; the
  service marks it in the list, refuses to delete it, still allows restoring it, and refuses a
  new checkpoint taking its name; the operator's actors never request approval while `mcp`
  does; the page's console and checkpoint actions cross the envelope. `tests/test_webui_contract.py`
  is new: it parses the page for `call("…")`, `action("…", "…")` and `api().…` and fails if a
  name it finds is missing from `HyperVService` or `Api`, if any name is private, or if the page
  names a VM of this household.
- Still open: waves 3–5 (file manager, lab/debugger, matrix), the `docs/DASHBOARD.md` rewrite,
  and the console cadence measurements for `docs/PERFORMANCE.md`.

### 2026-10-29 — dashboard wave 3: the file manager, host roots and the guest half

- **The host side is four roots, not a disk.** `service/files.py` (`FileService`) browses
  `lab`, `evidence`, `transfer` and `media` — `HOST_ROOTS` in `service/_base.py` maps each
  name to its setting — and every path is resolved and then checked against the resolved
  root, so `..` and an absolute path elsewhere both fail with the same refusal. The page
  gets `file_roots` (name, path, whether it exists) and draws its picker from that rather
  than from a hard-coded list.
- **A guest directory listing, a folder, a rename and a delete — on both transports.**
  `guest_list_dir`, `guest_make_dir`, `guest_rename` and `guest_delete` are structured guest
  operations: a `switch` arm in `transport/guest_operations.ps1` for SSH and the matching
  block in `HyperVBridge.ps1` for PowerShell Direct, with both `GUEST_OPERATIONS` sets
  updated. The duplication is the established pattern for the guest half, and
  `tests/test_transport_conformance.py` now holds the four to one contract over both.
- **Deleting is the only thing that needs a second look.** `host_delete` and `guest_delete`
  take `confirm_path` and refuse anything that is not the exact path, and a non-empty
  directory additionally needs `recursive=True`; the guest script refuses the same tree
  without the flag, so the confirmation is not the only barrier. Neither root itself can be
  renamed or deleted.
- **A transfer says how far it got.** `ProgressReporter` was already on the bus; the
  transports now take an `on_progress` callback (SFTP's `put`/`get` callback, and the two
  ends of a `Copy-Item` for PowerShell Direct, which reports nothing in between and does
  not pretend otherwise). `_TransferProgress` throttles that to one event per whole percent
  in the 5–90 band, leaving 99 for the verified digest — a 100 MB file is thousands of
  blocks and none of them deserves a line in the feed.
- **Transfers may start anywhere under a configured root.** `guest_put_file` used to accept
  only `transfer_root`; a staged package lives under `lab_root`, so the window copying it in
  one hop would have needed a detour through the transfer folder. `_confined_to_roots`
  replaces `_confined_path` and allows any of the four, and
  `test_guest_put_file_stays_inside_a_configured_root` documents both the refusal and the
  widening.
- **Nine tools, one opt-in family.** The file manager is its own `files` toolset: neither
  `safe` nor `lab` pays for nine schemas a conversation would carry for a task the operator
  does from the window, and `all` is 64 tools.
- Tests: `tests/test_files.py` (root confinement, directory-first ordering, idempotent
  mkdir, rename refusals, confirm-gated delete, recursive refusal, the progress band and its
  throttle), the four operations over both transports in the conformance file, and the
  service-layout test now counts `files` as a domain and reads only `*Service` classes so a
  private helper's `__init__` is not mistaken for a method with two homes.
- **The page is two panels and a bar.** The Files tab lists both sides side by side: the root
  picker is drawn from `file_roots` (never from a list in JavaScript), each panel keeps its own
  path with an Up button, and every row offers what its side allows — Open for a directory,
  “To guest →”/“To host →” for a file, Rename inline, and a Delete that only acts on the
  second click. The overwrite box supplies the `confirm_path` the service demands, so the rule
  is still the service's; the page only says which path it means. The progress bar reads the
  `progress` events the transfer already publishes, so it cannot drift from the Activity page.
- Tests: the page names only methods `HyperVService` has, checked by `tests/test_webui_contract.py`
  for the eleven file-manager names as for every other button.
- **Live, halfway.** `tests/live_files.py` ran against `NORTH-Test` from this shell: `file_roots`,
  `host_make_dir` and a recursive `host_delete` all succeeded, so the host half is validated on a
  real machine, and a `guest_delete` with the wrong `confirm_path` returned `not_authorized` before
  reaching the guest. The guest calls came back `access_denied`: the shell was not elevated, which
  is the shell and not the service. The guest half — mkdir, upload with its digest, listing,
  download, rename and recursive delete — still needs one run from an elevated prompt.
- Still open: waves 4–5, a light theme for the window, live numbers for the console cadence, and
  that elevated run of `tests/live_files.py`.

### 2026-10-29 — v0.3 dashboard wave 4: receipts, the live cycle and the crash triage

- **A receipt is a read, so its read side is a module, not a tool.** `lab/receipts.py` opens the
  `lab-cycle.json` files `lab/report.py` already writes: `list_receipts` sorts them newest first by
  the time the cycle finished (a record with no time sorts last, because an unknown time is not a
  recent one) and puts a directory holding no readable record in `skipped` instead of guessing at
  it; `get_receipt` names each evidence file by its host path and digest; `compare` walks the
  twenty fields two runs can differ on.
- **`absent_on` is the reason `compare` exists.** A run that bugchecked has a blamed driver and a
  run that passed does not, and reporting that as "changed from MyDriver.sys to nothing" would be a
  worse answer than saying the second run never had one. Each difference therefore carries which
  side lacks the field, and a field both sides lack is not compared at all.
- **A run id is checked before it is joined.** `_RUN_ID` allows only `[A-Za-z0-9_.-]`, which `..`
  and `.` pass, so `_run_id` also refuses a name that is nothing but dots — the two directory names
  that would leave the evidence root. An evidence entry whose path contains `..` is dropped from
  the listing for the same reason.
- **The record now keeps the triage, not just its summary.** `lab/report._crash` retains
  `bugcheck_str`, the parameters, the top frames, `sources` and kd's log path, so a receipt opened
  weeks later answers "where did it die" without the dump being read again. `receipts.crash_view`
  hands that to the window in the shape the triage panel draws.
- **`lab_receipts` is one tool with three actions.** It is a read of the host and touches no VM;
  `list`, `get` and `compare` share one implementation of what a record means, and the `lab` profile
  is 43 tools with the budget unchanged (`safe` stays 29, `all` is 65).
- **A cycle is one blocking call, so its phases come from the bus.** `LabService._tracked` wraps
  `lab_run`/`lab_cycle` in one `EVENTS.operation` and hands the step a `ProgressReporter`, and
  `lab/run.py` reports the share of wall time each phase usually takes — the guest's execution is a
  single blocking call and has no progress of its own to report. `_band` maps a nested step's 0–100
  onto a slice of the cycle's scale, so the two never publish two competing scales.
- **The page draws the service's numbers.** The Lab tab shows the newest `lab_cycle`/`lab_run`
  event's phase and percentage, the receipt list with pick-two-to-compare, the open receipt with a
  jump into the file manager per evidence file, and the crash triage formatted from `!analyze -v`
  (code, name, bucket, blamed driver, symbols, parameters, top frames, dump and kd log). The
  Debugger tab drives `kd_status`/`kd_attach`/`kd_break`/`kd_go`/`kd_command`/`kd_breakpoint`/
  `kd_break_on_load`/`kd_inspect`'s reads, the serial reader as a loop that stops when the page
  moves on or the read fails, and the prepare-guest buttons — each one service call, with the
  password and the KDNET key generated and stored by the service and never sent to the page.
- **One reader per pipe, said out loud.** `kd_attach` detaches the serial reader when it takes
  COM1, so the page reports which of the two owns the pipe and disables the read it knows the
  service would refuse.
- Tests: `tests/test_lab_receipts.py` (listing and its order, the VM filter, skipped directories,
  path-shaped run ids, a missing root, evidence paths that climb out, the not-found answer, a
  comparison and its `absent_on`, identical runs, a missing side, the evidence hash, and the
  service's action and argument checks); `tests/test_lab_run.py` gains the phase scale, the nested
  band and the one-operation-three-phases assertions; `tests/test_lab_report.py` asserts the stack
  survives into the record; `tests/test_webui_contract.py` covers the new buttons because every one
  of them names a real service method.
- Gate: 923 passed / 1 skipped, ruff clean, 13 PowerShell files parse, 0 analyzer findings, smoke
  29 / 43 / 65.
- Not validated live: the wave's own definition of done — a `lab_cycle` run from the window with a
  debugger attached and a bugcheck triaged on screen — needs a payload, and running one remains a
  separate operator decision. What is proved without a VM is the reading: the receipt, the
  comparison and the triage shape are covered by fakes.

### 2026-10-29 — v0.3 install wave 1: one routine, and the questions it asks first

- **One routine, three doors.** `setup.py` holds the install: the signed installer, `winget` and
  `scripts/install.ps1` are meant to end in the same `first_run`, so there is one answer to "what
  does an install do on this machine". `hyperv-setup` is that routine's command; `hyperv-install`
  stays what it was, the low-level writer of one client's file.
- **The host is asked before anything is written.** `read_host` reads `EditionID`/`ProductName` from
  the registry and the Hyper-V feature's `InstallState` from `Win32_OptionalFeature` — the CIM class
  rather than the DISM cmdlets, because it answers without elevation and the pre-check has to run
  before the one UAC prompt. `assess` turns that into `proceed`, `enable-hyperv` or `stop` plus one
  sentence; Windows Home is refused by name with the editions that work, and a feature that is
  installed but off stops with the exact `Enable-WindowsOptionalFeature` line.
- **`unknown` is not a failure.** A machine that cannot answer the feature query still gets an
  install; the self-test at the end is the authority on whether Hyper-V works, and a pre-check that
  blocked on its own blindness would be the more common failure.
- **The routine never reboots.** Enabling the feature returns `stopped: hyper-v` with the command;
  `install.ps1` elevates for that one step, enables with `-NoRestart`, registers a `RunOnce` entry
  that re-runs the script at the next sign-in, and says so. A script that restarts a machine without
  being asked is not an installer.
- **Clients are found by their own folder.** A fresh Claude Desktop has `%APPDATA%\Claude` and no
  config file, which is exactly the case worth catching; `detect_clients` checks the directory and
  `configure_clients` writes only those. With none found it prints the entry and the file each
  client keeps it in, instead of leaving the operator to read the source.
- **The self-test separates the two failures.** `event_snapshot` answers from the service's own bus
  and touches no VM, so a failure there is "nothing is listening" (start `hyperv-service`), while a
  failure in `host_status` is "the pipe answered and Hyper-V did not" (check the feature and the
  Hyper-V Administrators group). Two failures, two fixes, one line each.
- **Uninstall asks about the operator's own data.** `hyperv-setup --uninstall` takes our entry out
  of each client file, removes the shortcut and stops; the Credential Manager entries and the state
  folder are kept unless `--remove-credentials`/`--remove-data` are given, and the answer names both
  flags. A file it cannot parse is left exactly as it is. `known_vms` returns the spelling the access
  file records, because the vault's target is built from the VM's name and deleting another case
  would report a removal that did not happen.
- **Small refactors the routine needed.** `install._backup`/`_write_atomic` became `backup`/
  `write_atomic` (two modules now copy a file before touching it), and `config.data_root()` names the
  folder the uninstaller has to point at, which `_local_root` was building inline.
- Tests: `tests/test_setup.py` (30) — the Home refusal and its sentence, the enable command, absent
  and unknown, the probe mapping, the non-Windows host, detection by folder, the snippet when there
  is no client, the self-test's two failures and its two ready messages, the routine stopping before
  it writes anything, stopping for the restart rather than taking it, a second run reporting
  `unchanged`, uninstall removing only our entry and only our TOML section, refusing an unparsable
  file, keeping credentials and data unless asked, and the wrapper passing only flags the routine
  accepts. Nothing here needs Windows, Hyper-V or a VM: the OS answers through `probe`, `exists` and
  `environ`.
- Gate: 953 passed / 1 skipped, ruff clean, 13 PowerShell files parse (12 plus the new wrapper), 0
  analyzer findings on `scripts/install.ps1`, smoke 29 / 43 / 65 unchanged — the routine adds no MCP
  tool, which is the point: an install step an AI could call would be a way to make itself a
  permanent client.
- Validated live: `hyperv-setup --check` on this host answered `Windows 10 Pro has Hyper-V ready`,
  `action: proceed`, exit 0, reading the registry and `Win32_OptionalFeature` unelevated.
- Not built yet, and not claimed: the signed `.msi`/`.exe`, the `winget` manifest, the PyInstaller
  bundle and the beginner README. The first-run wizard is wave 2, below. The Windows service under a dedicated
  account is the largest of them and is not a one-line change: the pipe's DACL grants only the
  current user, `_verify_server_process` requires the server to be that user's interpreter,
  `Settings.from_env()` has no environment to read inside a service, and SCM needs a dispatcher this
  codebase does not have. Today the host is a per-user process started on demand by the first client
  and verified as a peer, which is the safer default until that design is settled.
- Not validated: the routine has not run on a clean Windows machine, so "installs on a fresh host"
  is still a claim about code, not about a machine.

### 2026-10-29 — v0.3 install wave 2: the first run, in three questions

- **The wizard closes the gap the install leaves open.** A configured install still leaves a window
  that can list every VM and use none of them, because a VM is reachable only once it has a level.
  The three questions are the VM, the level and the guest password once — `webui/wizard.py` decides
  them, `Api.wizard`/`Api.wizard_finish` are the two calls the page makes, and the page draws the
  form. No rule in JavaScript: the contract test still holds every name the page uses to a real
  method.
- **"Done" is derived, not recorded.** `status()` answers `needed` from the operator's access file —
  whether any VM has a level — rather than from a flag the wizard would write when it finishes. A
  flag keeps saying "done" on a machine where the level was later removed; the derived answer brings
  the wizard back, which is the behaviour a first run needs and a stale file cannot give.
- **No VM is not a first run.** With Hyper-V reporting nothing there is nothing to ask about, so the
  wizard does not open; it says why in `blocked` and the fleet already explains where VMs come from.
- **The password is one step, and skippable.** A VM can be driven from the host with nothing running
  inside it, so a first run that stopped on a password the operator does not have would be a first
  run that failed. Skipping says so, Guest adds it later, and **Store and test** finishes with
  `guest_credential_status` — the same call the Guest page makes, so the wizard adds no backend.
- **The stale-window bug this exposed, and fixed.** The service replaces its own `Settings` on
  `reload_policy`, but the bridge held the instance it was built with: a level written from the
  window kept reading as "no level" in that window until it was reopened, and the wizard — which asks
  the same question — would have opened again on the next launch. `Api._reread()` takes the level and
  baseline maps from the operator's file after each write, and only those two: the environment the
  window was started with is not re-read.
- Tests: `tests/test_webui_wizard.py` (18) — the three questions and their order, a level matching a
  VM whatever the case, the no-VM case that must not open, the level list with a sentence each, the
  service refusing the VM list, the level and password written where they belong, the finish without
  a password, a bad level stopping at its own step, a refused credential naming its step, the wizard
  not coming back once a level exists and coming back when it is removed, and the password never
  appearing in an answer. Plus `test_a_level_written_here_shows_up_in_this_window` in
  `tests/test_webui.py` for the staleness fix. All of it runs without a window or a service.
- Gate: 973 passed / 1 skipped, ruff clean, the page's script parses under `node --check`, contract
  test green (every `call("…")` a `HyperVService` method, every `api().…` an `Api` method), all 14
  PowerShell files under `src`, `tests` and `scripts` parse and the analyzer finds nothing in
  `scripts`, smoke unchanged — the wizard adds no MCP tool, because a tool that could configure
  access would be the AI writing its own policy.
- Not validated live: the wizard has not been walked through on a real host with a real guest, and
  the "Store and test" answer against a live PowerShell Direct session is still a claim about code.
  Still to come in this wave: the bare-command and tray launcher, the PyInstaller build and release
  CI, the beginner README. The bare-command launcher is wave 3, below.

### 2026-10-29 — v0.3 install wave 3: one command, two doors

- **The bare command is the window; a pipe gets the server.** `hyperv-console-mcp` is now
  `cli:main`, and with no arguments it decides by stdin: an MCP client always hands the process a
  pipe, so a pipe runs the server, while a terminal — or a shortcut's windowed interpreter, which
  has no stdin at all — opens the window. One entry point means a client's existing config and the
  Start Menu shortcut cannot disagree about what the command means.
- **The guess has explicit forms.** `--stdio` runs the server whatever stdin looks like;
  `--ui`/`--dashboard` open the window; `--vm NAME`, `--vm=NAME` and `--classic` are handed to the
  dashboard unchanged because they are its own flags; `-h`/`--help`/`--version` are answered without
  opening anything; an unrecognised argument is not refused, since a client that sends one is asking
  for the server.
- **`--ui` is dropped, not forwarded.** The first draft handed the whole list to the dashboard, whose
  parser knows only `--vm` and `--classic`: `hyperv-console-mcp --ui` would have exited with
  argparse's "unrecognized arguments" on a flag the launcher itself prints in its help. `WINDOW_ONLY`
  names the two words that stop at the launcher.
- **The shortcut's name matches what the install says.** `SHORTCUT_NAME` is `Hyper-V Console.lnk`;
  `setup.py` already tells the operator to "Open 'Hyper-V Console' from the Start Menu", so the file
  the old name created was one they had been told not to look for.
- **The dashboard takes its arguments as a parameter.** `dashboard.main(argv=None)` parses what it is
  given instead of always `sys.argv`, so the launcher can forward them; `hyperv-dashboard --vm X`
  behaves as before.
- Tests: `tests/test_cli.py` (23) — the four readings of stdin (a console, a pipe, none, a handle
  that raises), the bare command under each, `--stdio` under all three, every window flag and what it
  forwards, an unknown argument going to the server, `--version` and both help spellings opening
  nothing, and the entry point in `pyproject.toml` naming the launcher so the shortcut's bare command
  cannot drift back to the server. `test_the_shortcut_lands_in_the_start_menu` now asserts the name
  the message uses, and `test_a_frozen_build_points_the_shortcut_at_itself` covers the half of the
  shortcut that will only exist once there is a PyInstaller build.
- Gate: 997 passed / 1 skipped, ruff clean, smoke 29 / 43 / 65 unchanged — the launcher adds no MCP
  tool — all 14 PowerShell files parse, 0 analyzer findings on `scripts`.
- Not validated live: the shortcut has not been created and double-clicked on this host, and the
  frozen half of the guess (a windowed build with no stdin) is a claim until the PyInstaller bundle
  exists.
- Still to come in this wave: the tray icon, the PyInstaller build and release CI, the beginner
  README. The tray icon is wave 4, below.

### 2026-10-29 — v0.3 install wave 4: the icon in the notification area

- **The tray is a client, not a second program.** `tray.py` opens the window with
  `window_command()` — the launcher's own `--ui` — and reads `host_status` and `list_vms`, the two
  calls the window makes on open. No rule of its own, and no state beyond the process it started.
- **A host that will not answer still gets an icon.** When `tray_service()` refuses, the icon is
  created anyway with a line that says why. The tray is how an operator notices that state; the
  alternative was an exit code nobody sees.
- **The tooltip names no VM.** `status_text` counts and names the profile — `3 VMs · 2 running · lab
  profile` — and adds `Hyper-V is off` when the host says so. A tooltip sits on whatever desktop the
  tray is on, so the line is fleet-shaped; a test asserts a VM name cannot appear in it.
- **One click, one window.** `open_window` keeps the process it started and does nothing while it is
  alive, so a second click does not leave two windows behind; a window opened from the Start Menu is
  not counted, because that is not this tray's business. The child is detached, so closing the tray
  does not close the window.
- **Optional, and it says how not to be.** `pystray` is an extra (`hyperv-console-mcp[tray]`),
  imported inside `main()`; without it the command names the extra and exits 2. Pillow, which
  pystray needs for the image, was already a dependency, and the glyph is drawn rather than shipped,
  so no package data was added.
- Tests: `tests/test_tray.py` (15) — the line for three VMs, for one, with Hyper-V off, and with no
  VM name in it; the two calls it reads and a service that refuses; the command and its frozen form;
  the double click and the freed click once the window closes; the drawn image; the missing extra;
  and the three items doing what they are named, with the private service closed on exit and an
  icon that survives a host which will not answer. `pystray` is faked through `sys.modules`, so none
  of it needs the package, a desktop or Hyper-V.
- Gate: 1012 passed / 1 skipped, ruff clean, smoke 29 / 43 / 65 unchanged, 14 PowerShell files
  parse, 0 analyzer findings on `scripts`.
- Not validated live: `pystray` is not installed on this host, so the real icon, its menu and its
  notifications have not been seen. What is proved is the wiring against the documented API —
  `Icon(name, icon, title, menu=…)`, actions called with `(icon, item)`, `notify(message, title)`,
  `update_menu()`, `stop()`, `run()` — and a fake would pass just as happily if that API were
  different. The first run of `hyperv-tray` with the extra installed is the test that closes it.
- Still to come in this wave: the PyInstaller build and release CI, the beginner README.

### 2026-10-29 — v0.3 install wave 5: one folder, four programs, and a pipe that trusts it

- **The four program names live in one module.** `frozen.py` holds `LAUNCHER`, `SETUP`, `HOST`
  and `ACCESS`, and the spec, the Inno script and the three places in the code that name a
  program all read them. The three places need three *different* siblings: the client entry must
  be the launcher (naming the installer would reinstall the machine every time the AI starts),
  the shortcut must be the launcher (naming the installer would re-run the installer from the
  Start Menu), and `_spawn` must be the host (naming the launcher would start a second server on
  a pipe nobody reads).
- **One analysis, four executables.** The spec analyses the four entry scripts once and splits
  the script table per `EXE` with `scripts_for()` — runtime hooks plus that one program. Handing
  the whole table to an `EXE` would put four entry points in one bundle.
- **Four shims, because each has a reason.** `packaging/entrypoints/*.py` import their module
  absolutely: the launcher is `__main__`, where relative imports fail; the host has to exist
  beside the launcher or a client opens a window with nothing answering; and the refusal
  messages name `hyperv-access`, so it ships.
- **The installer runs the routine, not a second installer.** `installer.iss` is per-user
  (`PrivilegesRequired=lowest`, `{localappdata}\Programs\Hyper-V Console`) with a fixed `AppId`,
  and its last page runs `hyperv-setup.exe --no-shortcut`; uninstall runs the same program with
  `--uninstall`.
- **The workflow ships tags and says when it cannot sign.** `.github/workflows/release.yml`
  builds on `v*`, signs with `signtool` only when `WINDOWS_CERT_PFX_BASE64` is present, and
  emits a `::warning::` naming SmartScreen when it is not. It submits no winget manifest: a
  manifest that has never been through `winget validate` should not be.
- **winget's uninstall key is Inno's `AppId`.** `packaging/winget/` carries the three manifests
  and a test reads the `AppId` out of the `.iss` and requires `{…}_is1` in the installer
  manifest — the failure that leaves an old version installed after an upgrade.
- **The live run found the bug the tests could not.** `ipc._verify_server_process` accepted only
  `python.exe`/`pythonw.exe`/`py.exe`, so the frozen service — `hyperv-service.exe` — was
  refused with *"The service host is not a Python process; refusing to trust the pipe"* and
  every tool call in a frozen install failed. `_trusted_image()` now accepts a Python image, or
  `hyperv-service.exe`/`HyperVConsoleMCP.exe` **in the same folder as the running program** — a
  copy elsewhere is not the program the installer wrote.
- **Two more things the build itself corrected.** The spec imported the package before
  `Analysis` set `pathex` up, so `pyinstaller` died with `ModuleNotFoundError`; and the help
  text went out in the locale's code page, which turned the em dash into a lone byte in a pipe —
  `cli._say()` writes stdout as UTF-8, which is what the server writes on that stream anyway.
- Tests: `tests/test_packaging.py` (14) — the spec parses, freezes exactly the four names with
  only the launcher windowed, every console script is shipped or named as left out
  (`hyperv-lab`, `hyperv-dashboard`, `hyperv-tray`, `hyperv-install`), each shipped stub imports
  the `main` its entry point names, the installer installs what the spec builds, installs
  without elevation, runs the routine's own flags, the workflow builds both artifacts and only
  on tags, and the winget manifests describe one package at the current version with the
  installer's `AppId`. `tests/test_frozen.py` grew to 8 (the two `_trusted_image` cases), and
  `tests/test_cli.py` to 24 (the UTF-8 help).
- Gate: 1036 passed / 1 skipped, ruff clean, smoke 29 / 43 / 65 unchanged, 14 PowerShell files
  parse, 0 analyzer findings on `scripts`, `node --check` on the page's script.
- **Validated live on this host** (PyInstaller 6.22.3): the spec builds
  `dist/HyperVConsoleMCP/` — 160 MB, four executables; `HyperVConsoleMCP.exe --version` and
  `--help`; an MCP `initialize` handed to the launcher over a pipe, answered as
  `hyperv-console-mcp 0.1.0`; `tools/call hyperv_host_status` answered through the service the
  launcher started, whose process is `dist\HyperVConsoleMCP\hyperv-service.exe`;
  `hyperv-setup.exe --check` reporting `action: proceed`; `hyperv-access.exe --help`.
- **Not validated live.** The bundle was built without `pywebview` and `pystray` (neither is
  installed here), so this folder's window is the tkinter one and the tray extra is absent; CI
  installs `.[dev,ui]`. Inno Setup is not installed, so `installer.iss` has never been
  compiled and no `Setup.exe` exists. The workflow has never run, nothing has been signed, and
  **no install has been tried on a clean machine** — that is wave 6's test, not a claim made
  here.
- **The beginner page is text, not pictures.** `docs/QUICKSTART.md` is the install for someone
  who has never opened PowerShell: one copyable line that answers "can this machine run it",
  the download, the three questions the window asks (`vm`, `level`, `credential` — the order
  `webui/wizard.py` asks them in), a table of the five failures that actually happen with the
  fix beside each, the SmartScreen box explained, and how to uninstall. The section's GIFs and
  wizard screenshots are **not** in it: there is no installer binary to capture them from and
  the wizard has never been opened on a clean machine, so the page describes the window instead
  of linking to an image that does not exist.
- Still to come in this wave: those screenshots, after the first clean-machine install. The
  service under `HyperVConsoleMCPSvc` is decided against, below.

### 2026-10-29 — the service stays per user, and why that is not a shortcut

The install section asked for the host to run as a Windows service named
`HyperVConsoleMCPSvc`, in *Hyper-V Administrators*, with `SeServiceLogonRight`. That would be a
machine-wide process, and this product is not one. Five things break at once, and four of them
are the security model:

1. **The pipe is current-user-only.** `ipc._security()` builds a DACL that grants this user and
   nobody else. A service account is not that user, so either the service cannot be reached or
   the DACL has to widen to a group — and then every member of that group can drive Hyper-V
   through it.
2. **The peer check refuses it.** `_verify_server_process` requires the server's token to carry
   the client's user SID. A service account fails that check by design; the check is what makes
   a guessable pipe name safe.
3. **The secrets are the user's.** The guest password and the KDNET key live in *this user's*
   Windows Credential Manager under `HyperVConsoleMCP/<vm>`. A service account has its own
   vault and cannot read it; moving them to a machine-wide store puts a guest's password where
   every administrator can read it — the opposite of the "segredo nunca em claro" rule.
4. **The state is the user's.** `%LOCALAPPDATA%\HyperVConsoleMCP` holds events, transfers,
   evidence, receipts and logs. A service account would write them to its own profile, where the
   operator who paid for the run cannot see them.
5. **The configuration is the user's.** `Settings.from_env()` reads the environment the client
   is started with; a service has none, so every setting would move into a machine-wide file —
   and the second user of the same machine would inherit the first one's allowlist.

So the answer is not a dispatcher. The host stays a per-user process, started on demand by the
first client (`ensure_running()` → `_spawn`) and verified as a peer. It already outlives the
client that started it, which is the property the service was wanted for; what it does not do is
start at boot, and a boot-start service is precisely the shape that cannot hold a user's
secrets.

If the host should be up before anyone opens anything, the shape that keeps all five properties
is a **per-user logon task** — `schtasks` with the user as principal, no elevation, the user's
environment, the user's vault — rather than a service. That is a small addition to the routine
and is not built here, because nothing in the wave needs the host running before the first
client asks.

What would change this decision: a deployment that genuinely serves several users of one
machine from one host. That is a different product — a shared service with a per-user policy
store and a per-user secret store in front of it — and it should not be grown out of this one by
widening a DACL.

### 2026-10-30 — v0.3 dashboard wave 5: a matrix, a recording and a light that says which path

- **A recording is sampled, not streamed.** `lab/recording.py` runs one thread that asks the
  console for a frame every two seconds and keeps at most sixty; past that it **halves** the kept
  set and doubles the stride, so the picture still spans the run's wall time instead of ending at
  the moment the cap was hit. The writer thins again until the GIF is under 8 MiB, and the report
  says how many were sampled and how many survived. `console_frame_size`/`console_frame` were added
  to `ConsoleService` with `audit=False` per frame: a frame is not an operation, and sixty of them
  in the audit log would bury the run that produced them.
- **The recording had to be staged outside the run's directory.** `DriverLabTransfer.collect`
  creates its destination with `mkdir(exist_ok=False)`, so writing the GIF into the run's evidence
  folder from the host would have raced the collector that owns it. The recorder writes to
  `<root>/.recording-<run_id>` and `file_recording()` moves it in afterwards, which also means a
  cycle that fails mid-way leaves a `.recording-` directory rather than a half-written evidence
  entry. `lab_cycle(record=true)` and `lab_matrix(record=true)` both go through that path.
- **A matrix is a loop with a receipt per row, not a new kind of run.** `lab/matrix.py` takes two
  to four VMs (`MAX_VMS = 4`), gives each one `<run_id>-<vm>` and its own checkpoint, and runs the
  ordinary cycle sequentially — each cycle owns the guest worker, the checkpoint, the debugger and
  the console for its whole length, so running them in parallel would be four processes claiming
  one pipe. A VM that refuses is a row carrying its error code, not the end of the matrix, and one
  approval names every VM so the operator approved the set they are looking at.
- **`compare` is the same idea as `lab_receipts`, over `FIELDS`.** `("outcome",
  "duration_seconds", "bugcheck_code", "blamed_driver", "symbols_all_loaded", "recording")` is what
  two runs of one package can honestly differ on; a field both rows lack is not compared, so "no
  recording either way" is not printed as agreement and a bugcheck that has no blamed driver is not
  printed as a change from one.
- **The transport switch is its own tool because a read cannot be a switch.** `guest_report` is
  `read_only_hint`, and a `transport=` argument on it would make that annotation a lie the first
  time someone saved a choice through it. `guest_transport` is a separate mutation, gated like the
  others; `guest_transport_status` stays the read and gained `configured` (what the next call would
  use) beside `saved` (what the operator decided).
- **`auto` deletes the record instead of writing a third value.** `transport/choice.py` keeps
  `%LOCALAPPDATA%\HyperVConsoleMCP\transport.json`, and choosing `auto` removes the VM's row: the
  file then states only decisions the operator made, and an absent row and a row saying "auto" can
  never disagree about what "no preference" means. Precedence is environment override → saved
  choice → `settings.guest_transport`, and the answer names which one won.
- **The probe's latency is measured around the call that produced it.** The first version started
  the clock before the health check and read it after building the answer, so the number included
  the bookkeeping. It is now read immediately after `ssh.health()` returns, in both branches, and a
  probe that fails is an answer with `healthy: false` and a message rather than an exception that
  would take the whole status down.
- **The page draws both, and draws nothing it computes.** The Matrix page ticks the VMs, runs
  `lab_matrix` and paints the service's comparison — one row per VM, then a field × VM table with
  the differing rows marked, which is the wave's definition of done as a screen. Outside the lab
  profile it shows the manual-run explanation instead of a button the service would refuse. The
  Overview shows the transport light: one `guest_transport_status(probe=true)` beside the host
  reads, latency, what is configured versus what is saved, and a select that calls
  `guest_transport_set`. The receipts table gained a symbols column and an "Export all" button
  (`lab_receipts(action="export")`, written under `exports/`).
- Tests: `tests/test_lab_matrix.py` (the VM normalisation and its refusals, per-VM run ids, the
  comparison and its absent fields, a refusing VM as a row, the service's argument checks);
  `tests/test_lab_recording.py` (the stride past the cap, the byte bound, the staging directory, the
  move into the evidence set, a frame that cannot be read costing one frame); `tests/test_guest_transport.py`
  (17 — the probe and its latency, a red probe as an answer, `probe=false` sending nothing, both
  paths on a provisioned VM, the saved choice surviving a second service instance, `auto` removing
  the row, a bad row ignored while the good one counts, an unreadable file loud); `tests/test_server.py`
  and `tests/mcp_smoke.py` for the surface; `tests/test_webui_contract.py` because every new button
  names a real service method.
- Gate: 1080 passed / 1 skipped, ruff clean, 13 PowerShell files parse, 0 analyzer findings, smoke
  30 / 45 / 67.
- Not validated live: every number this wave produced is synthetic. The recording has only ever
  sampled generated PNGs, never a real `GetVirtualSystemThumbnailImage`; the matrix has never run
  two real VMs; `guest_transport_status` has never probed a real guest; and the switch has never
  written to a real profile from the window. What is proved without a VM is the reading and the
  refusal paths.

### 2026-10-30 — v0.3 dashboard closing round: parity in the other direction, and a page a keyboard can drive

The waves were done; what the roadmap's global definition of done still owed was the properties
that no single wave produces — parity checked in both directions, a page testable without a
WebView, and a window that survives a light theme and a keyboard.

- **Parity was only ever checked one way.** `test_webui_contract.py` proved every name the page
  calls exists on `HyperVService`; nothing proved the converse, that an operation the operator can
  click is one the AI can ask for. `mcp_service_methods()` now AST-walks `server.py` for
  `service.<attr>` and the test fails if a page operation is in neither that set nor a batch. It
  found a real gap on the first run: the receipts table asks `kd_symbols` for the per-driver badge
  and no tool asked it, so `kd_inspect` grew `what="symbols"` with the `module` it is about rather
  than a tenth kd tool. A second test reads the `ConsoleAction` discriminator's literals to prove
  the five per-action console calls really are the actions `console_batch` takes, so the mapping is
  checked rather than asserted in a comment.
- **A fake answers the whole contract.** `tests/test_webui_fake_service.py` gives `Api` a service
  that answers any public name with the name and the arguments it was given, and drives every name
  the page calls through it. The interesting half is the refusals: an unknown name and a wrong
  argument set are `invalid_argument` answers that name the method, because the page draws
  refusals and a traceback would be a blank window, and a private name (`_approve`) never crosses
  the bridge at all.
- **Two palettes, one set of names.** The colours moved out of the rules and into a dark `:root`
  block plus a `:root[data-theme="light"]` block that redefines the same names. `test_webui_page.py`
  compares the two sets in both directions and scans the rest of the stylesheet for a hex literal —
  the first version of that scan failed on ten colours I had written straight into the new button
  rules, which is exactly the rot it exists to catch. The only allowed literal is `#000`, the
  guest's own screen, because that is a picture of a display and not a surface of this window.
  `applyTheme()` reads a saved choice from the window's `localStorage` and otherwise follows
  `prefers-color-scheme`, listening for the system changing under it; `color-scheme` is set per
  theme so scrollbars and form controls follow. The choice never reaches the service.
- **The keyboard reaches every control.** Tabs became two real buttons (the name opens, `×` closes,
  both labelled), the nav marks the current page with `aria-current`, and a receipt row became a
  `role="button"` with a tab stop and an Enter/Space handler — a table row that responds to a click
  had otherwise been a control only a pointer could reach. The console image is `role="application"`
  and keeps a visible `:focus-visible` outline it had been removing. The test that holds this is a
  scan for `el("div", {… onclick …})`: an element that is not an interactive tag and does not
  declare a role, a tab stop and a key handler together is a pointer-only control. It failed on
  `img` and `tr`, which is how those two got promoted.
- **The layout stops stretching and stops overflowing.** `.grid` gained `align-items: start` and
  the transport card a two-row span, which closed the hole the grid had been leaving under the
  Overview's short cards; the Fleet table moved to a full-width card because its power buttons
  wrapped in a third of the row; wide tables scroll inside their own card (`scroller()`) rather
  than pushing the page sideways. Below 1100px it is one column.
- Tests: `tests/test_webui_page.py` (nine — the two palettes, no colour outside them,
  `color-scheme` per theme, the system-following default, visible focus, the narrow layout, the
  pointer-only scan, the tabs, the live regions); `tests/test_webui_fake_service.py` (five);
  `tests/test_webui_contract.py` (three, the parity ones); `tests/test_server.py` for
  `kd_inspect`'s new action and its required `module`.
- Gate: 1098 passed / 1 skipped, ruff clean, `node --check` on the page's script clean, smoke
  30 / 45 / 67.
- Not validated live: the page has been exercised with a stub bridge in Edge headless and with a
  fake service in pytest, never in WebView2 against a real service; the light theme and the
  keyboard paths have been rendered and scanned but not used by a person. `kd_inspect(what="symbols")`
  has not been asked of a real kd session.

### 2026-10-30 — v0.3 dashboard: what the window opens, and a drop that is the same copy

Two of the roadmap's items were still sentences in a document rather than things the window could
do: "ofereça um botão 'abrir no Hyper-V Manager / VMConnect'" and "arrastar e soltar entre os
painéis dispara a cópia".

- **The buttons open the real viewers, and the window decides which.** `Api.open_vmconnect` and
  `Api.open_hyperv_manager` live on the bridge rather than on the service because the service is a
  background process: a window it started would belong to that process's desktop, not the
  operator's. `open_vmconnect` asks the service for `list_vms` first and refuses a name the host did
  not report, so the allowlist still stands between a name and a process, and the name goes in as
  one argument — `["localhost", "Lab VM"]`, never a command string — so nothing re-parses it. A host
  without the management tools answers `unavailable` with the path it looked for.
- **A drop is the row's own copy.** `acceptDrops` puts the dragover/drop handlers on each panel and
  the drop calls `copyEntry`, so there is still one implementation of each direction; a test asserts
  that the page contains exactly one `guest_put_file` and one `guest_get_file` call site. Only files
  drag, because the service copies one file at a time and a folder would need a recursive transfer
  it does not have. A drop back on the panel it came from is ignored, and the target panel is drawn
  with a dashed accent outline while the drag is over it.
- **Two layout defects the screenshots found.** The file table's actions column had been sized by
  `table-layout: fixed` at roughly a third of the panel, so three buttons stacked into three rows;
  the columns are now sized (kind 66px, size 60px, actions 232px) and the buttons keep to one line.
  And the console's "Console info" line was a `<p>` of unwrapped JSON that widened the whole page
  into a horizontal scrollbar; it is a `pre.log` now, which wraps inside its own 300px box.
- Tests: six in `tests/test_webui.py` (the viewer's command line, a name with a space staying one
  argument, an unreported VM refused without starting anything, a missing VM name, an uninstalled
  viewer as an answer, Hyper-V Manager opening the `.msc`), and one in `tests/test_webui_page.py`
  (the drop reaching `copyEntry`, one call site per direction, the drop outline in the palette).
- Gate: 1105 passed / 1 skipped, ruff clean, `node --check` clean.
- Verified in a browser, not in WebView2: with a stub bridge in headless Edge, a dispatched drag from
  a host row onto the guest panel produced `guest_put_file` with `local_path C:\Lab\MyDriver.sys`
  and `guest_path C:\DriverLab\MyDriver.sys`, the reverse produced `guest_get_file`, and a
  same-panel drop produced no call and left no highlight. The two buttons were rendered and their
  commands asserted in pytest; **neither `vmconnect.exe` nor `mmc.exe virtmgmt.msc` has actually
  been started by this window on a real host.**

### 2026-10-30 — v0.3 dashboard: the upload takes the VMBus when the guest offers it

Onda 3 asks for "upload por `Copy-VMFile` quando disponível, senão sessão PS Direct/SFTP", and the
tree only had the `senão`: every upload was `Copy-Item -ToSession`.

- **`guest_put_file` prefers `Copy-VMFile`.** The bridge probes
  `Get-VMIntegrationService -Name 'Guest Service Interface'`; when it is enabled the bytes cross the
  VMBus with `Copy-VMFile -CreateFullPath -Force -FileSource Host` instead of the remoting session.
  A service that cannot be queried counts as absent, so a host or guest that never had the interface
  keeps the old path rather than failing a probe.
- **The fallback is the fallback, not the error.** An enabled interface can still refuse the copy —
  the guest service may be stopped — so a `Copy-VMFile` that throws is written to the verbose stream
  and the session copy runs instead. The guest's own `GuestFileState` digest still decides whether
  the upload worked, so neither path is trusted on its own.
- **The reply says which one ran.** The bridge's result carries `via` (`copy-vmfile` or `psdirect`).
  The Python side does not need it — `FileResult` is built from path, size and digest — but it is
  what the three new tests assert, so the branch is pinned rather than inferred.
- Downloads are untouched: `Copy-VMFile` is host-to-guest only, so `guest_get_file` stays on
  `Copy-Item -FromSession`.
- Tests: three in `tests/test_bridge_regressions.py` — enabled (and the session copy mocked to throw,
  proving it did not run), enabled but refusing (falls back and still verifies), and disabled (the
  session path, with `Copy-VMFile` mocked to throw if it is ever attempted). The first run of them
  failed because the mock declared `$CreateFullPath` as a value parameter, so `-CreateFullPath` bound
  with no argument and every upload silently took the fallback; that is exactly the failure mode the
  fallback is there for, and it is why the tests assert `via` rather than only the digest.
- Docs: `docs/PERFORMANCE.md` and `README.md` now describe both paths instead of only the session
  copy; `docs/DASHBOARD.md` says the page does not care which carried the upload.
- Gate: 1108 passed / 1 skipped, ruff clean, PSScriptAnalyzer clean over `src`, MCP stdio smoke green.
- **Not validated live:** no upload has been attempted against `HYPERV_MCP_LIVE_TEST_VM` on this
  path, so whether a real guest accepts `Copy-VMFile` — and whether it is actually faster — is
  unmeasured. `scripts/bench_transport.py` is where that number would come from.

### 2026-10-30 — v0.3.0

The first tag on the `v0.3` branch. `__version__` moves from `0.1.0` to `0.3.0`, which is what the
branch, `docs/ROADMAP-v0.3.md` and the wave reports had been calling this work; the package had
never been renumbered as the waves landed, so `hyperv-console-mcp --version`, the wheel, the Inno
installer and the winget manifests all still said `0.1.0`.

- `src/hyperv_console_mcp/__init__.py` is the single source: `pyproject.toml` reads the version from
  it, the release workflow passes it to `ISCC.exe`, and `tests/test_packaging.py` asserts the three
  winget manifests carry it. Bumping one file without the other two is what that test exists to
  catch, and it caught nothing here because all three moved together.
- The winget `InstallerUrl` now points at `v0.3.0`'s asset. `InstallerSha256` stays the all-zero
  placeholder: it is the hash of an installer that does not exist until the workflow has built it,
  and `packaging/winget/README.md` says to fill it before the PR to `microsoft/winget-pkgs`. **No
  winget PR is opened by this release.**
- README's "1036 unit/transport tests" is 1108, the number this tree actually runs.
- Local build verified: `python -m build` produced `hyperv_console_mcp-0.3.0-py3-none-any.whl` and
  `.tar.gz`, both carrying `webui/assets/index.html`, the bridge `.ps1` files and the transport
  scripts; `--version` answers `0.3.0`.
- Gate before the tag: 1108 passed / 1 skipped, ruff clean, PSScriptAnalyzer clean over `src`.
- **What the tag does not mean.** `.github/workflows/release.yml` has still never run, so the frozen
  programs, the Inno installer and the draft release are built on GitHub for the first time by this
  tag; nothing is signed (no certificate in secrets), so SmartScreen will warn. Live validation
  against `HYPERV_MCP_LIVE_TEST_VM` remains outstanding for the dashboard page in WebView2, the
  `Copy-VMFile` upload, and the viewer buttons.

### 2026-10-30 — the Linux job runs the suite for the first time, and it did not

The release workflow's first two attempts failed on Windows; the third passed both Windows jobs and
failed the Linux one with 30 real failures — the first time the suite had ever run off Windows. None
of them was a test that needed Hyper-V; each was a place where the code or the test had quietly
assumed the host's platform.

- **26 of the 30 were one bug in `credentials.resolve`.** It reads the Credential Manager first and
  falls back to the environment, but on a host without a vault the read raised `PolicyError`
  ("only available on Windows") instead of reporting nothing stored, so the fallback was
  unreachable and every guest call on Linux died before it looked at the environment. `resolve` now
  reads that one error as "nothing stored" and takes the environment; `source()` already did. The
  store itself keeps saying what it is — `write` and `delete` still refuse on a host with nowhere
  to put the secret, and a vault that exists but will not answer still raises `BackendError`. The
  platform check moved into `_vault_available()` so a test can stand in for the other host without
  patching `sys.platform`.
- `test_frozen.py` built its paths with backslashes, so on POSIX `Path(...).name` was the whole
  string and a python image was not recognised; written with forward slashes it splits on both hosts.
- `test_kd_service.py::test_dump_open_runs_kd_and_parses` faked `run_dump` but not `find_debugger`,
  so it depended on kd.exe being installed on the runner; it now patches discovery the way the
  file's own `patched` fixture does.
- `test_secrets.py::test_the_ssh_private_key_stays_in_its_file` wrote its state to `ssh_root/VM`
  while the code reads `ssh_root/vm_key(VM)`, which is casefolded — the same directory on Windows,
  a different one on Linux. It now uses `vm_key`.
- Gate: 1116 passed / 1 skipped on Windows, ruff clean, MCP stdio smoke green. The Linux job is the
  evidence that the fix worked, and it is not claimed until it runs.

### 2026-10-30 — v0.3.0 is published, and the pipeline that publishes it has now run

The tag was moved from `acd5803` to `2190d24` — the four commits of CI fixes came after it — and
the push built everything the earlier entry could only promise. Nothing was lost by moving it: the
only previous release run (`41005477544`) died at its test step, so no release and no asset existed
to overwrite.

- **The pipeline was proven before the tag was spent.** `release.yml` carries `workflow_dispatch`
  for exactly this, so it ran by hand on `2190d24` first: tests, PyInstaller, `python -m build`,
  Inno Setup, artifact upload — all green, with the draft step correctly skipped. Only then did the
  tag move. This is the first time `ISCC.exe` has compiled `installer.iss` anywhere, so the
  installer had never existed before this run.
- **The tag run** (`41010753965`) then produced `Hyper-V Console v0.3.0` as a **draft**, with
  `HyperVConsoleMCP-Setup-0.3.0.exe` (66.5 MB) and `hyperv_console_mcp-0.3.0-py3-none-any.whl`
  attached. The sdist is built and uploaded as a workflow artifact but is not a release asset; the
  release's two files are what the workflow's `gh release create` names.
- **`InstallerSha256` is now filled**: `4f95b17d…f17ef5`, the hash of the asset at the release URL.
  An Inno installer embeds a timestamp, so the hand-run build hashed differently (`569b7b36…`) and
  would have been the wrong value — the reason the placeholder waited for the tag. A new test,
  `test_the_winget_hash_is_a_hash_or_the_placeholder`, accepts the all-zero marker or a real
  SHA-256 and nothing in between: winget accepts a malformed manifest, then refuses every download
  with a checksum mismatch, which is worse than saying "not filled in yet".
- **The release is a draft on purpose** and stays one until it is reviewed: nothing is signed, and
  `winget validate` and a PR to `microsoft/winget-pkgs` are still open. **No winget PR is opened
  here.**
- `main` was fast-forwarded to `2190d24` as well, because it is the repository's default branch and
  had been nine hours behind `v0.3` — the repository page, the commit graph and "last updated" all
  read `main`, so the work looked stalled while it was landing.
- Gate: 1117 passed / 1 skipped, ruff clean, MCP stdio smoke green; the Linux job of the tests
  workflow is green on the same commit for the first time.

## Install wave report

**What was delivered, by section.** §0: nothing to edit by hand — the wizard asks the three
questions that need a person (`vm`, `level`, `credential`) and writes the same `access.json` and
the same stored password the operator tools write. §1: three paths, in the order a beginner
should meet them — the installer on the releases page, `winget install
maiconzeraxd.HyperVConsoleMCP`, and `scripts/install.ps1` documented as the advanced one. §2:
the runtime is inside the download; `packaging/hyperv-console-mcp.spec` freezes four programs
into one folder and the release workflow builds them on a tag. §3: `hyperv-setup` does the
pre-check (edition, Hyper-V state, resumable after the restart), writes each detected client's
config, adds the Start Menu shortcut, runs the self-test that names the failing step, and is
idempotent — a second run reports `unchanged`. §4: one command, one click and a tray. §5:
`--uninstall` removes the entries and the shortcut, and keeps credentials and data unless asked.
§6: `docs/QUICKSTART.md`. §7: the build is ready to sign — `signtool` runs in CI when the secret
is present and a `::warning::` names SmartScreen when it is not; Hyper-V is a pre-check answer,
never a stack trace. §8: `tests/test_packaging.py` keeps the spec, the installer, the workflow
and the winget manifests from drifting apart.

**Definition of done, audited.**

| Item | State |
|---|---|
| Download one thing → open → it works | **Partly.** The frozen folder runs on this host; the `.exe` installer around it has never been compiled. |
| Three install paths | Installer script and winget manifests exist; neither has been run end to end. `scripts/install.ps1` is the only one exercised. |
| Embedded runtime | **Done.** PyInstaller 6.22.3 built it here and the four programs ran. |
| Install routine: pre-check, one UAC, service, clients, shortcuts, self-test, idempotent | **Done except the SCM service**, decided against with the reasoning above. The installer asks for *no* elevation at all, so it is one UAC fewer than promised. |
| One click / one command + tray + wizard | Done. |
| Uninstall | Done in the routine; the Inno uninstaller that would call it has not been compiled. |
| Beginner README with GIFs | **Text only.** No installer exists to capture screens from. |
| Ready to sign | Done in CI; nothing has been signed. |
| Hyper-V as a hard prerequisite with a readable message | Done and validated on this host (`action: proceed`). |
| Tests and CI | Done; the release workflow has never run. |
| Offline install finishes once downloaded | **Untested.** The bundle carries its own interpreter and data, but no machine has been taken offline and installed on. |

**Not validated live, and therefore not claimed.** No clean Windows machine has run the
installer, because there is no installer: Inno Setup is not installed here, so `installer.iss`
has never been compiled. The release workflow has never run on GitHub. Nothing has been signed.
The winget manifests have never been through `winget validate`. The bundle built here was built
without `pywebview` and `pystray`, so its window is the tkinter one and its tray is absent — CI
installs `.[dev,ui]`. Those five are the first things to close after this wave, in that order:
compile the installer, run it on a clean machine, then sign, then submit the manifest.

## Phase 4 report

**What changed.** `hostwmi/` now serves 30 host operations over COM/WMI (`vm_get`, power, checkpoints,
firmware and Secure Boot, media, console input, thumbnails, serial preparation, host crash events,
job status), each with its own module and tests; anything not in `HANDLERS` falls back to the
PowerShell worker with `wmi_unsupported`, so the migration stayed incremental. The bridge keeps the
PowerShell Direct work: one reused `New-PSSession` per VM (`Get-GuestSession`, dropped when the
uptime says the guest rebooted) and `Copy-Item -ToSession`/`-FromSession` in both directions with a
SHA-256 check. `GuestSplitBackend` runs guest operations on a second worker, so a long guest command
no longer blocks `vm_get`, the console or the dashboard. On the guest side, `lab/publish.py` owns
every package decision (`guest_package_inspect`/`guest_package_list` return facts only),
`DriverLab.Common.psm1` lost its canonical-JSON implementation, `Test-DriverLabPackage.ps1` hashes
the manifest file, `guest_config.ps1` reports raw registry/BCD/memory facts with
`transport/guest_config.py::derive_live` computing the view, and `Get-DriverLabDiagnostics.ps1`
reports the BCD tokens it read with `lab/diagnostics.py` deciding them.

**Tests added across the phase.** WMI handler tests with a fake COM connection, the publish/verify
split (including a re-indented manifest being refused), the SSH provisioning upload of
`guest_operations.ps1` and the per-process execution-policy bypass, the session-reuse and
`Copy-Item` round trips, the live-view derivation, and the diagnostics derivation with the pt-BR
bcdedit fixture. Suite: 641 passing, 1 skipped.

**Validated live on `NORTH-Test`.** WMI power/checkpoint/firmware paths; the guest session being
reused, a reboot opening a new one, and the second worker holding; a 100 KB `guest_exec` and a
100 KB file over both transports; package staging, host-side verification, a tampered file being
named, and the publish refusal over PowerShell Direct and SSH; `guest_config_status` with the
derived view.

**Still pending, and why.** `ssh_network_prepare`/`ssh_network_remove` remain in the bridge: they mix
Hyper-V objects (the internal switch, the adapter) with host network-stack calls that are not WMI,
so they cannot move as one operation without splitting the command in two. `HyperVBridge.ps1` is
still 1 776 lines, most of it the guest operations PowerShell Direct requires. The crash classifier
(`bugcheck` versus `hang` without a debugger) is still covered only by unit tests: inducing a
bugcheck means running a payload, which stays a separate operator decision.


## Phase 5 report

**What changed.** Zero-config discovery: `hostwmi/kvp.py` reads the guest's computer name, OS and IPs
from `Msvm_KvpExchangeComponent` with no credential, and `vm_get`/`vm_list` carry it as
`guest_facts`; the VM list, generation, Secure Boot, COM ports and DVD drives come from
`root\virtualization\v2`; `kd/discovery.py` finds the SDK debugger and `host_status` reports whether
this process can administer Hyper-V at all; `%LOCALAPPDATA%\HyperVConsoleMCP\` is created on demand.
One level per VM (`access.py`, `hyperv-access`) replaced the seven `HYPERV_MCP_ALLOW_*` variables as
the way to say what a VM may do, and the shipped defaults `--vm NORTH-Test` / `north-kd` are gone —
a test fails if either name comes back. A refusal now carries `fix` (`{tool, args}` or
`{operator}`), so a missing credential, a stopped SSH service or a running VM names the call that
closes the gap. `service/host.py` puts the service in one background process over a hardened named
pipe and `service/remote.py` gives the MCP server and both windows one envelope to reach it, with
`open_service` as the single factory. The window is a WebView2 page (`webui/`), and `install.py`
writes a client's MCP configuration and a Start Menu shortcut.

**Tests added across the phase.** KVP parsing and its no-guest case, the level-to-capability map and
its precedence over the flags, the fix contract with source sweeps that every named tool exists and
every argument matches its schema, the pipe's framing and its refusals, the proxy and dispatch
envelope, the page's API surface and the page's own constraints (no `http://`, no remote asset), and
`hyperv-install`'s create/merge/refuse/unchanged paths. Suite: 762 passing, 1 skipped.

**Validated live on this host and `NORTH-Test`.** KVP `guest_facts` (`DESKTOP-AA9PSTJ`, its OS and
IPs) through the WMI backend; the single service holding one worker across processes, with a client
that finds a host joining it instead of starting a second; the level file read by a running service
after `hyperv-access` wrote it; `hyperv-install` in a throwaway `USERPROFILE` against the real console
script, including a refused invalid file and a real `.lnk`; and the window itself — eight pages
rendering, the VM chooser, an approval card answered from the page, and a level change greying out
guest and debugger.

**Still pending, and why.** The window has not been driven against a *live* guest: this shell is
unelevated, so Hyper-V reads return `access_denied` and the capture path needs an elevated session;
the pages were rendered over a fake service and the real one, but no screenshot of `NORTH-Test` has
been drawn in it. A real MCP mutation held open while an operator clicks *Approve once* in the
window also waits on a human. `uvx hyperv-console-mcp` was not exercised as a launch path — the
console scripts from a real install were, which is the same entry point.

## Phase 6 report

**What changed.** Driver QA that a lab iteration can actually use. `transport/guest_verifier.ps1` and
`transport/guest_verifier.py` turn Driver Verifier on and off from the registry, record what they
replaced, and restore it on `Revert`; `lab_verifier` exposes that and the read side is in
`lab_diagnostics`. `transport/guest_driver_service.ps1` and `guest_driver_service.py` install, start,
stop and remove a test kernel-driver service through advapi32, reporting the Win32 error and the
driver's NTSTATUS; `lab_driver_service` exposes that. `lab/crash.py` triages the dump a bugcheck run
already collected — newest minidump first, `!analyze -v` through the existing kd engine, merged with
the host's event-18590 record, with `kd-analyze.log` kept beside it. `kd/symbols.py` parses `!lmi`
and `lm` so the cycle can say whether the test driver's symbols loaded, and `analyze_report` derives
the same verdict from a dump. `lab/report.py` writes `lab-cycle.json` beside the evidence: package,
manifest hash, checkpoint and revert, outcome, crash reading, symbol result and one hash over the
evidence files. The evidence index now carries `modified_utc` per file, which is what makes "newest
dump" mean this run's crash.

**Tests added across the phase.** `tests/test_guest_driver_service.py` (18): argument shape, path and
name validation, the Win32-to-NTSTATUS mapping, unsigned-driver reporting, no verdict for a read, the
confirm/approve split, and four Windows-only probes against the real SCM. `tests/test_lab_crash.py`
(14): dump choice, escaping index entries, the merge with and without kd, the triage paths and a
missing evidence root. `tests/test_kd_symbols.py` (14): `!lmi` and `lm` parsing including a rejected
PDB, the module-name refusal, `symbols_resolved` from an analysis, and the read with its `lm`
fallback. `tests/test_lab_report.py` (10): the record's contents, the evidence hash, a refused run id
and an unwritable host. Cycle-level tests in `tests/test_lab_run.py` cover a crash without a debugger
being triaged, a run that did not crash never touching kd, symbols being reported per driver, and the
record being written. Suite: 840 passing, 1 skipped.

**Validated live.** The driver-service program's P/Invoke was exercised against this host's real
Service Control Manager — `OpenSCManager`, `OpenService` and `QueryServiceStatusEx` on `EventLog`,
which needs no elevation — and returned `running` with its state code. That is the only part of the
phase that touched a real Windows component; everything else ran against fakes.

**Still pending, and why.** Nothing in this phase has been run against `NORTH-Test`: enabling the
Verifier, installing a test driver service and a real bugcheck with a dump to triage all need an
elevated shell and a payload, and running a payload stays a separate operator decision. The optional
IOCTL robustness harness was not built — it is marked optional in the roadmap, and a malformed-input
generator behind an AI-callable tool is the one item here whose shape deserves the operator's own
sign-off rather than a phase default; the same loop is expressible today inside the operator's
`Run-Lab.ps1`, which the lab already runs with a checkpoint.

## Phase 7 report

**What changed.** The phase is mostly the work that makes the earlier phases checkable. In
`tests/test_secrets.py`, the scan now runs against a deliberately leaking build and against one
planted file per scanner, so a green run means the scan looked and found nothing rather than that it
found nothing to look at; the fixture also stopped reading — and writing — the operator's real event
directory, which it had been doing because a directly constructed `Settings` takes the field default
rather than the environment variable. `hyperv_host_status` publishes the registered tool count per
toolset, `tests/mcp_smoke.py` starts the `lab` profile as well and asserts all three surfaces per
family, and `tests/test_server.py` asserts the same table, which is how the README's stale `lab = 43`
was found and corrected to 42. The workflow's Linux job grew from an import check into ruff plus the
unit suite. On the documentation side, `docs/DRIVER-TESTING.md` is new, `SECURITY.md` gained the
single-service threat model and the phase 6 surface, `docs/LAB.md` links the new flow, and the
window's Lab page describes the cycle it can now run instead of a runner it never had.

**Tests added across the phase.** Two in `tests/test_secrets.py`: the leaking build and the
per-scanner planted files. One in `tests/test_server.py`: the host report's counts and total. The
per-toolset table is also asserted in `test_every_tool_belongs_to_a_known_toolset`, against the tool
names parsed out of `server.py`, so a tool added to a family without the count moving fails there.
Suite: 843 passing, 1 skipped. The MCP smoke passes for 29, 42 and 55 tools.

**Validated live.** The MCP smoke over a real stdio client, three times, with the per-toolset counts
coming back from a running server: `safe` 29, the `lab` profile 42, every toolset 55, each with its
mutation refused. The Linux job is **not** validated live: there is no distribution installed under
WSL on this host, so the claim that the suite runs clean on Linux rests on the `skipif` guards and on
every `ctypes.WinDLL` call being inside a function, which the import step exercises — CI is where it
gets proved.

**Still pending, and why.** Nothing here needs a payload, so the phase's own items are all run. The
one thing this phase could not close is the live validation the earlier phases still owe: an elevated
run on `NORTH-Test` confirming `outcome: bugcheck` rather than `hang` with no debugger attached, and
the phase 6 Verifier, driver-service and crash-triage paths against a real guest. Both need elevation
and a payload, and running a payload remains a separate operator decision.

## v0.2 Definition of Done, audited

The v0.2 phases were closed before the v0.3 round. This is that roadmap's Definition of Done
checked against the tree as it stands, so each box rests on an artefact rather than on the
roadmap's own optimism. Re-audited 2026-10-29 on `v0.3`.

| Item | State | Where it is checked |
|---|---|---|
| Every Fase 1 fix has a regression test | met | `tests/test_bridge_regressions.py`, `tests/test_lab*.py`, `tests/test_events.py`, `tests/test_ipc*.py` |
| pytest, ruff, PSScriptAnalyzer and the MCP smoke green on Windows | met, re-run | 857 passed / 1 skipped, ruff clean, 13 PowerShell files parse, 0 analyzer findings, smoke 29 / 42 / 55 |
| `psdirect` and `ssh` produce equivalent results on one transport test set | met, added by this audit | `tests/test_transport_conformance.py` — one assertion set over both; breaking the SSH truncation fails only the `ssh` case |
| Autologon, then a thumbnail screenshot of the desktop with no human input | met, live | the automation-toggles entry above: an interactive session plus a captured desktop with no login prompt |
| `lab_cycle` end to end without clicks, with checkpoint, evidence and revert | met, live | `tests/live_cycle.py`: `outcome: "ok"`, `revert.restored: true` |
| `kd_attach` → `kd_break` → `kd_stack` → `kd_go` live | met for serial, open for KDNET | `tests/live_kd.py --transport serial`; `--transport kdnet` needs a guest reboot and has not been run |
| `kd_dump_open` + `kd_analyze` on a real dump | open | `tests/fake_kd.py` proves the parser, the merge and the symbol verdict; no real dump has been read |
| No secret in a result, event, audit record or log | met | `tests/test_secrets.py`, including the deliberately leaking build and one planted file per scanner |
| `docs/PROJECT.md` says what was and was not validated live | met | the live entries above, each carrying its own "not validated live" line |

**What this audit changed.** The third row had no artefact: each transport was proved against
its own fakes, so nothing asserted that the same call produces the same result whichever
transport serves it — the property the lab actually depends on. `tests/test_transport_conformance.py`
now runs one set of assertions (result keys, the transport name in every result, truncation at
the configured limit, a failing command reported as a failed result, a put/get round trip with
matching digests, and a structured operation refused before the guest is touched) over both
transports, through `HyperVService` rather than through the transport classes, because the tool
result is what the operator sees. It was proved able to fail: removing the SSH truncation fails
`test_exec_truncates_at_the_configured_limit_on_both_transports[ssh]` and leaves the `psdirect`
case green.

**Still open, and why.** Three boxes need something this repository does not supply: KDNET
live needs a guest reboot and a reboot is a separate operator action; `kd_dump_open` needs a
real dump, which needs a bugcheck, which needs a payload; and the bugcheck-versus-hang
classification with no debugger attached needs both. Running a payload remains a separate
operator decision, and no tool here loads a driver.
