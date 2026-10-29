# Local dashboard

The native Windows dashboard observes and controls the same policy-gated service used by the MCP server. It does not open an HTTP listener and does not bypass capability flags, the VM allowlist, or exact confirmation fields.

## Start

Run from an elevated PowerShell when Hyper-V management requires elevation:

```powershell
python -I scripts\run_dashboard.py
```

`--vm <name>` picks the VM up front. Without it the window opens the only VM Hyper-V reports on this host, and asks from a list when there is more than one — nothing in this program names a VM of its own. When `HYPERV_MCP_ALLOWED_VMS` is set it narrows that list, so an allowlist of one still opens without a prompt.

After installation, `hyperv-dashboard` is equivalent. Use the same protected environment as the MCP host; a `.env` file is not loaded automatically.

The debugger page's pipe name is derived from the chosen VM (`<vm>-kd`, lowercased, with anything the provider rejects turned into a hyphen), so two VMs configured from the same installation never share one.

The sidebar shows the chosen VM's access level next to its name, and the capability list below it is that VM's — not the host-wide flags — so a `read-only` VM reads as read-only here even under the `lab` profile. Levels are set with `hyperv-access set "<vm>" read-only|control|lab`; see the README for what each one allows.

## Pages

- **Overview:** host and VM state, capability indicators, guest diagnostics and explicit power/checkpoint actions.
- **Activity:** operation actor, phase, progress and error code. Events from MCP and dashboard processes are read from the local event directory.
- **Driver lab:** fixed-tool initialization, package staging, hash verification and evidence collection.
- **Guest access:** the credential the host keeps in the Windows Credential Manager for this user, with buttons to set it locally, rotate it, test PowerShell Direct and SSH, and forget it. The password is typed into the dashboard, written straight into the vault, and never reaches the AI, the event log or the audit record. Below it, one row per automation setting (autologon, lock screen, Ctrl+Alt+Del, power and screen blanking, UAC consent, automatic update reboot), each with `leave` / `on` / `off`, plus an optional restart that waits for a desktop session.
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

Override it with `HYPERV_MCP_EVENT_DIRECTORY`. Both MCP and dashboard must use the same directory. Events are appended as one JSON line per record to `events-YYYYMMDD.jsonl`, under a process lock so MCP and dashboard can write concurrently; the dashboard tails those files by byte offset instead of re-reading the whole history every poll. `HYPERV_MCP_EVENT_RETENTION_DAYS` (default 14) deletes older daily files the first time a process writes in a day.

Records contain operation metadata only; they exclude guest commands, typed text, credentials and file contents. Local event files may reveal VM names and operation history, so protect the profile directory normally.

## Optional approval of AI mutations

Set this in both the MCP and dashboard environments:

```powershell
$env:HYPERV_MCP_REQUIRE_DASHBOARD_APPROVAL = 'true'
```

When enabled, every mutating MCP tool waits for one local decision. The gate sits at tool level, not at bridge level: one `lab_stage` asks once for the whole package instead of once per file and chunk, one `console_input` asks once for the whole batch, and nested calls inherit the approval. Read-only tools such as `lab_verify`, `lab_diagnostics`, `guest_report` and `events_snapshot` never ask. Local `serial_connect`, `serial_send`, `serial_disconnect` and `console_screenshot` requests pass through the same gate.

A request becomes a card in the window's right-hand queue instead of a modal prompt. The queue appears when something is waiting and leaves when the last card is answered, so several requests stay readable at once and the window keeps working while an operation is held: the operator can read the VM's state, another page, or the previous run before deciding. A card is bound to the broker's request id, and it disappears on its own when the broker stops waiting, so a timed-out request cannot leave a live-looking button behind.

The current-user-only named pipe carries a request ID, operation, VM name and a secret-free summary built by the tool: the power action, checkpoint name or ID, guest path with size and SHA-256, package ID with file count and manifest hash, or the first 20 lines of a `guest_exec` command plus the SHA-256 of the full command. The card renders that summary in a scrollable panel; typed text and serial input are reported only by length, so a password never reaches the broker. Each response applies to one request; timeout, refusal or an unavailable broker denies the operation before it reaches the backend or local action. The first pipe instance is created with `FILE_FLAG_FIRST_PIPE_INSTANCE`, so a second broker cannot share the name, and the client verifies the server process is a Python interpreter owned by the current user before sending anything.

Dashboard-originated actions use their own confirmation dialogs. Exact `confirm_*` values in the MCP schema are consistency checks against accidental parameter mismatch, not independent proof of human intent.

The dashboard never invokes the manual lab runner and never loads a driver or mapper.
