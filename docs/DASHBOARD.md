# Local dashboard

The dashboard is a **thin client of the same service the MCP server uses**. It draws a page; it does not decide anything. Every button is a call to a `HyperVService` method through the same `dispatch` envelope the named pipe carries, so a page cannot reach past the allowlist, the per-VM level, the protected baseline, or a `confirm_*` field — those rules live in Python, and the AI meets them at the same door.

Two consequences are worth stating plainly:

- **No business rule lives in JavaScript.** The page composes paths and maps pixels; it does not decide what is allowed. When the window needs something the service does not offer, the service grows a method — which is also what makes the feature reachable by the AI through an MCP tool, because the two share it.
- **No secret reaches the page.** The guest password, the SSH key and the KDNET key never enter the WebView, its console or its logs. The window asks for a rotation, a test or a revert; the value is generated, stored and used inside the service.

The window is one HTML file with its CSS and JavaScript inline, loaded through pywebview with no local HTTP server and no port. It needs the Evergreen WebView2 runtime (present on current Windows 10/11 and with Edge) and `pywebview` (`pip install hyperv-console-mcp[ui]`). Without either, the classic tkinter window opens instead; `--classic` asks for it on any host.

## Start

Run from an elevated PowerShell when Hyper-V management requires elevation:

```powershell
python -I scripts\run_dashboard.py
```

`--vm <name>` picks the VM up front. Without it the window opens the fleet, and nothing in this program names a VM of its own. When `HYPERV_MCP_ALLOWED_VMS` is set it narrows what is listed, so an allowlist of one still opens without a prompt. After installation, `hyperv-dashboard` is equivalent. Use the same protected environment as the MCP host; a `.env` file is not loaded automatically.

## The window

The **fleet** is the home page: every VM Hyper-V reported, its state, the level this host allows it, CPU and memory, and the power actions that level permits. Opening a VM creates a **workspace tab**; several may be open at once and only one is active. A workspace is the full set of per-VM pages:

- **Overview** — the VM's state, the level it is held to, and what the AI may do with it.
- **Activity** — the live feed: actor, operation, VM, phase, progress and error code, for the AI and for this window, read from the same event bus. This is where the operator watches the AI work.
- **Console** — the guest display as an image you can click and type into.
- **Checkpoints** — the tree of states, with the operator's baseline marked and locked.
- **Recovery** — power and checkpoint actions, including the forced ones that ask for a confirmation.
- **Media** — an ISO, the DVD drive and boot order.
- **Debugger** — COM1 serial, the kd session and the symbols it resolves.
- **Guest** — the credential source, the automation toggles and one command.
- **Lab** — diagnostics, package staging, verification and the cycle that runs a package.
- **Files** — the host's four roots beside the guest's own disk.

Closing the window does not stop a VM or guarantee that an already-running guest command was cancelled, and changing pages does not cancel a background operation.

## The first run

A window on a host where nothing has been configured can list every VM and use none of them, because a VM becomes reachable only when it has a level. The wizard closes that gap in three questions: pick the VM, pick the level, type the guest password once. It opens by itself while no VM is reachable, and never again once one is — unless the operator asks for it from the fleet (**Set up another VM**), which is how a second VM is added.

It is not a fourth way into the policy. The level is written to the same access file the Overview page and `hyperv-access` write; the password goes to the service's `guest_credential_set`, which stores it in the Windows Credential Manager; the VM list comes from `list_vms`; **Store and test** finishes with `guest_credential_status`, the same call the Guest page makes. What the wizard asks, and whether it should open at all, is decided in `webui/wizard.py` — in Python, against the service, and tested without a window.

Two decisions are worth naming:

- **"Done" is derived, not recorded.** The wizard writes no flag saying it has run. It asks whether any VM has a level, which is a fact about the operator's file; a flag would go on saying "done" after the level was removed, and the wizard would never come back for the VM that still has none.
- **The password step can be skipped.** A VM can be driven from the host — power, console, checkpoints — with nothing running inside it, so a first run that stops at a password the operator does not have would be a first run that failed. Skipping says so, and Guest can add it later.

## What the operator can do that the AI cannot

The human is a trusted actor, and three things are only theirs:

- **Set or remove a VM's level.** `hyperv-access set "<vm>" read-only|control|lab` and the Overview page write the same file; no MCP tool reaches it. The level is the operator's decision about what the AI may do. The first-run wizard writes it through the same call.
- **Name the baseline checkpoint.** The protected checkpoint is recorded in the same access file. `delete_checkpoint` refuses it for every actor and `create_checkpoint` refuses to reuse its name, so the window and the AI are held to the same rule.
- **Answer an approval.** See below.

Everything else the window offers, the AI can do too, through the tool of the same name. That parity is enforced by a test: `tests/test_webui_contract.py` parses the page for the method names it calls and fails if one is missing from `HyperVService` or `Api`.

## The console is an image, not video

The console page renders `console_screenshot(source="thumbnail")` a few frames per second and sends clicks as guest-display pixels, mapping the `<img>`'s CSS box back through the `source_width`/`source_height` in the screenshot metadata. `capture_scale` and `coordinates_match_mouse` say whether that mapping is honest, and the page repeats the answer instead of guessing. Typing is batched into one `console_input` call, and a paste box covers OOBE, where the guest has no clipboard to share. For full fidelity open VMConnect from the same page: this is a picture of the display, not a video stream, and it cannot show a session the VMConnect desktop is drawing exclusively.

Capture stops for a reason: the loop pauses when the window is hidden or the operator paused it, and it *stops asking* — rather than retrying twice a second — when the service refuses, which is what an Off VM or a level without `control` looks like.

## The file manager

Two panels, one per side. The host panel is limited to the four roots this server was configured with — `HYPERV_MCP_LAB_ROOT`, `HYPERV_MCP_EVIDENCE_ROOT`, `HYPERV_MCP_TRANSFER_ROOT` and `HYPERV_MCP_ISO_ROOT` — and never to `C:\`: a path is resolved and then compared with the resolved root, so `..` and an absolute path elsewhere fail the same check. The guest panel reaches the lab machine's own disk through the configured transport.

Both sides list, create a folder, rename, delete and copy to the other side. Copying a file either way is `guest_put_file`/`guest_get_file`, so the SHA-256 is verified at the destination and the digest shown is the one the far side computed. Deleting takes two clicks on the same row and the service still requires the path spelled back; a non-empty directory additionally needs the "delete folders recursively" box, and the guest script refuses the tree without it too. Progress comes from the event bus: the bar reads the `progress` events the transfer publishes, so it shows the same numbers on the Activity page.

## The lab page

The lab page is built around one rule: a cycle is a single service call, so the page cannot know how far it got by watching a spinner. It reads the `started`/`progress`/`completed` events the service publishes for `lab_cycle` and `lab_run` — the same records the Activity page shows, under one correlation id — and draws the newest phase with its percentage. The numbers are the service's, not the page's estimate.

Running a package is offered only in the lab profile, because that is where the server accepts it. Staging (`lab_stage`) is a host-to-guest copy of a directory under `HYPERV_MCP_LAB_ROOT` with a manifest built on both sides; `lab_verify` compares the published package with an approved digest; `lab_collect` reads evidence without executing anything. The wave in the roadmap called this pair `lab_plan`/`lab_apply`; the service has no such methods and the page does not invent them, so the staging panel calls what exists.

**Receipts** are the read side of `lab-cycle.json`. `lab_receipts(action="list")` names every run under the evidence root, newest first and optionally for one VM; a directory holding no readable record is listed as skipped rather than guessed at. Clicking a row opens it: the record's fields, and every evidence file with its host path and SHA-256, each with a jump into the file manager. Clicking two rows and then "Compare" runs `lab_receipts(action="compare")`, which reports what matched and what changed, and marks a field one side never had as `absent` — a run that crashed has a blamed driver and a run that passed does not, and that is not the same as "changed to nothing".

**Crash triage** formats `!analyze -v` instead of showing it: the bugcheck code and its name, the bucket, the blamed driver, whether the symbols resolved, the parameters and the top frames, with the dump and kd's log named where they are. It reads either the report the cycle just returned or the receipt it wrote, because the record keeps the crash — the stack included — so a run can be triaged long after the window that ran it closed.

## The debugger page

kd is driven through `kd_attach`/`kd_status`/`kd_detach`, with `kd_break`, `kd_go`, `kd_command`, `kd_breakpoint` (set, clear, break-on-load) and the read-only `kd_stack`, `kd_modules`, `kd_analyze`, `kd_symbols` and `kd_dump_open`. The page shows the state the service reports and nothing else: an unattached VM says so, and a command that needs a session is offered only as a button the service will refuse rather than as one that looks armed.

The serial console reads COM1 in a loop through `serial_read`, replacing the tail on each tick rather than appending to it, and stops the moment the page moves on, the read fails or the operator stops it. One reader per pipe is the rule: `kd_attach` detaches the serial reader when it takes the pipe, so the page says which of the two owns it and does not offer a read that would be refused. Typed text goes through `serial_send`, which tells the approval card only how many characters it carried.

The "prepare the guest" buttons each fire one service call and show its result: guest BCD (`lab_guest_configure`), Driver Verifier (`lab_verifier`), autologon (`guest_autologon_configure`), password rotation and forget (`guest_password_rotate`, `guest_credential_forget`), KDNET (`debug_kdnet_prepare`, `debug_kdnet_deprovision`) and the COM1 pipe (`debug_serial_prepare`). The password and the KDNET key are generated and stored by the service; the page never receives them and has no field to type them into.

## Delivery waves

The dashboard was delivered in waves, each one gated by the full test suite before it was committed:

| Wave | Delivered |
|---|---|
| Transport | The page calls the service through the same envelope as the pipe; live events over the existing bus; a fake service the tests drive without a WebView. |
| 1 | The fleet, the live activity feed, the non-modal approval queue, host status and a connection indicator. |
| 2 | The clickable console, the checkpoint tree with a locked baseline, and in-place two-click confirmations. |
| 3 | The host↔guest file manager: four roots, both directions, digest and progress. |
| 4 | Lab receipts and comparison, the live cycle, the crash triage, the kd console and breakpoints, the serial reader and the prepare-guest buttons. |
| 5 | Multi-VM matrix, run recording and per-VM transport health. |

Wave 5 is not built yet.

## Cross-process activity

The default event directory is:

```text
%LOCALAPPDATA%\HyperVConsoleMCP\events
```

Override it with `HYPERV_MCP_EVENT_DIRECTORY`. Both MCP and dashboard must use the same directory. Events are appended as one JSON line per record to `events-YYYYMMDD.jsonl`, under a process lock so both processes can write concurrently; the window tails those files by byte offset instead of re-reading the whole history every poll. `HYPERV_MCP_EVENT_RETENTION_DAYS` (default 14) deletes older daily files the first time a process writes in a day.

Records contain operation metadata only; they exclude guest commands, typed text, credentials and file contents. Local event files may reveal VM names and operation history, so protect the profile directory normally.

## Optional approval of AI mutations

Set this in both the MCP and dashboard environments:

```powershell
$env:HYPERV_MCP_REQUIRE_DASHBOARD_APPROVAL = 'true'
```

When enabled, every mutating MCP tool waits for one local decision. The gate sits at tool level, not at bridge level: one `lab_stage` asks once for the whole package instead of once per file and chunk, one `console_input` asks once for the whole batch, and nested calls inherit the approval. Read-only tools such as `lab_verify`, `lab_diagnostics`, `guest_report` and `events_snapshot` never ask. Local `serial_connect`, `serial_send`, `serial_disconnect` and `console_screenshot` requests pass through the same gate.

The operator's own clicks do not ask themselves for permission: requests made by the window (`ui`/`dashboard`) are not queued, only those from the AI (`mcp`).

A request becomes a card in the right-hand queue instead of a modal prompt. The queue appears when something is waiting and leaves when the last card is answered, so several requests stay readable at once and the window keeps working while an operation is held: the operator can read the VM's state, another page, or the previous run before deciding. A card is bound to the broker's request id and disappears when the broker stops waiting, so a timed-out request cannot leave a live-looking button behind.

The current-user-only named pipe carries a request ID, operation, VM name and a secret-free summary built by the tool: the power action, checkpoint name or ID, guest path with size and SHA-256, package ID with file count and manifest hash, or the first 20 lines of a `guest_exec` command plus the SHA-256 of the full command. The card renders that summary in a scrollable panel; typed text and serial input are reported only by length, so a password never reaches the broker. Each response applies to one request; timeout, refusal or an unavailable broker denies the operation before it reaches the backend or local action. The first pipe instance is created with `FILE_FLAG_FIRST_PIPE_INSTANCE`, so a second broker cannot share the name, and the client verifies the server process is a Python interpreter owned by the current user before sending anything.
