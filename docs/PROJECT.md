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
