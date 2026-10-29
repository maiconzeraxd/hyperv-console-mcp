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
