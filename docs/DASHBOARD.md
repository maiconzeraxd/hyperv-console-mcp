# Local dashboard

The native Windows dashboard observes and controls the same policy-gated service used by the MCP server. It does not open an HTTP listener and does not bypass capability flags, the VM allowlist, or exact confirmation fields.

## Start

Run from an elevated PowerShell when Hyper-V management requires elevation:

```powershell
python -I scripts\run_dashboard.py --vm NORTH-Test
```

After installation, `hyperv-dashboard --vm NORTH-Test` is equivalent. Use the same protected environment as the MCP host; a `.env` file is not loaded automatically.

## Pages

- **Overview:** host and VM state, capability indicators, guest diagnostics and explicit power/checkpoint actions.
- **Activity:** operation actor, phase, progress and error code. Events from MCP and dashboard processes are read from the local event directory.
- **Driver lab:** fixed-tool initialization, package staging, hash verification and evidence collection.
- **Console:** confirmed synthetic keyboard and mouse input.
- **Recovery:** checkpoint inspection, creation, restore and deletion.
- **Media:** host ISO selection and mount/eject on an existing DVD drive; the VM must already be Off.
- **Debugger:** separate host COM1 and guest BCD preparation, serial attachment, output and confirmed serial input.

Closing the dashboard does not stop a VM or guarantee that an already-running guest command was cancelled. Changing pages does not cancel a background operation.

## Cross-process activity

The default event directory is:

```text
%LOCALAPPDATA%\HyperVConsoleMCP\events
```

Override it with `HYPERV_MCP_EVENT_DIRECTORY`. Both MCP and dashboard must use the same directory. Records contain operation metadata only; they exclude guest commands, typed text, credentials and file contents. Local event files may reveal VM names and operation history, so protect the profile directory normally.

## Optional approval of AI mutations

Set this in both the MCP and dashboard environments:

```powershell
$env:HYPERV_MCP_REQUIRE_DASHBOARD_APPROVAL = 'true'
```

When enabled, bridge mutations originating outside the dashboard wait for a local dialog. Local `serial_connect`, `serial_send`, `serial_disconnect` and `console_screenshot` requests pass through the same gate. The current-user-only named pipe carries a request ID, operation and VM name—not commands, payloads, typed text or credentials. Review the originating conversation for those details before approving. Each response applies to one request; timeout, refusal or an unavailable broker denies the operation before it reaches the backend or local action.

Dashboard-originated actions use their own confirmation dialogs. Exact `confirm_*` values in the MCP schema are consistency checks against accidental parameter mismatch, not independent proof of human intent.

The dashboard never invokes the manual lab runner and never loads a driver or mapper.
