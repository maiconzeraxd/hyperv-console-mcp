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
