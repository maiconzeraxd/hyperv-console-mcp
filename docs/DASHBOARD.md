# Local dashboard

The dashboard is a **thin client of the same service the MCP server uses**. It draws a page; it does not decide anything. Every button is a call to a `HyperVService` method through the same `dispatch` envelope the named pipe carries, so a page cannot reach past the allowlist, the per-VM level, the protected baseline, or a `confirm_*` field — those rules live in Python, and the AI meets them at the same door.

Two consequences are worth stating plainly:

- **No business rule lives in JavaScript.** The page composes paths and maps pixels; it does not decide what is allowed. When the window needs something the service does not offer, the service grows a method — which is also what makes the feature reachable by the AI through an MCP tool, because the two share it.
- **No secret reaches the page.** The guest password, the SSH key and the KDNET key never enter the WebView, its console or its logs. The window asks for a rotation, a test or a revert; the value is generated, stored and used inside the service.

The window is one HTML file with its CSS and JavaScript inline, loaded through pywebview with no local HTTP server and no port. It needs the Evergreen WebView2 runtime (present on current Windows 10/11 and with Edge) and `pywebview` (`pip install hyperv-console-mcp[ui]`). Without either, the classic tkinter window opens instead; `--classic` asks for it on any host.

## Start

Run from an elevated PowerShell when Hyper-V management requires elevation. The installed launcher asks for elevation itself, so a shortcut or a double-click still starts the window with the rights it needs:

```powershell
python -I scripts\run_dashboard.py
```

`--vm <name>` picks the VM up front. Without it the window opens the fleet, and nothing in this program names a VM of its own. When `HYPERV_MCP_ALLOWED_VMS` is set it narrows what is listed, so an allowlist of one still opens without a prompt. After installation, `hyperv-dashboard` is equivalent, and so is `hyperv-console-mcp` with no arguments — the same command a client's config names, which opens the window when a person runs it and serves stdio when a client does. Use the same protected environment as the MCP host; a `.env` file is not loaded automatically.

## In the browser: `--serve`

The same page can be opened in the operator's own browser instead of the WebView2 window:

```powershell
hyperv-console-mcp --serve              # prints the URL, opens the browser
hyperv-console-mcp --serve --port 8080  # a fixed port, for a bookmark
hyperv-console-mcp --serve --vm Lab-VM  # open on one VM
```

It prints `http://127.0.0.1:<port>/?token=<token>` and opens it. Three things are worth knowing:

- **The token is per run.** It is generated when the listener starts, printed with the URL, and
  required on every call; it is never written to disk. A URL without it answers 401, and a new
  run has a different one — so a bookmark needs `--port`, not a saved token.
- **The listener is loopback only.** It binds `127.0.0.1` and refuses a `Host` or `Origin` that is
  not a loopback name, which is what stops DNS rebinding and a page on another site from driving
  it. For another machine, tunnel to the loopback; nothing here listens on the network.
- **It is the same window.** Every call goes to the same `Api` the WebView2 page uses and so to
  the same service, the same policy and the same audit trail. What the page shows and what it
  refuses are identical in both modes; only the transport differs — the bridge in the window,
  HTTP here.

The page in a tab is a picture of the same thing, not a second implementation: the live feeds
(`events`, `logs`, `kd_output`) are the held-open service calls, so they stream over plain HTTP
long-polling and this mode needs no websocket.

## The notification-area icon

`hyperv-tray` (from `pip install hyperv-console-mcp[tray]`) is the same window one click away: **Open** starts it as a separate process, **Status** shows the host line, **Quit** closes the icon. The tooltip is the live answer — the number of VMs, how many are running, the profile, and `Hyper-V is off` or why the service is not answering — refreshed every twenty seconds and never naming a VM, because a tooltip sits on whatever desktop the tray is on. A host whose service will not start still gets an icon that says so: the icon is how an operator notices.

It decides nothing. It opens the launcher's own command, reads `host_status` and `list_vms` — the calls the window makes on open — and holds no state beyond the window it started, so a second click does not leave two windows behind. It is optional: without `pystray` the command says what to install and exits.

## The window

The **fleet** is the home page: every VM Hyper-V reported, its state, the level this host allows it, CPU and memory, and the power actions that level permits. Opening a VM creates a **workspace tab**; several may be open at once and only one is active. Two pages are about the host rather than about one VM and stay reachable with no workspace open — **Fleet** and **Matrix**. A workspace is the full set of per-VM pages:

- **Overview** — the VM's state, the guest transport with its green/red light and round-trip latency, the level it is held to, and what the AI may do with it.
- **Activity** — the live feed: actor, operation, VM, phase, progress and error code, for the AI and for this window, read from the same event bus. This is where the operator watches the AI work.
- **Console** — the guest display as an image you can click and type into.
- **Checkpoints** — the tree of states, with the operator's baseline marked and locked.
- **Recovery** — power and checkpoint actions, including the forced ones that ask for a confirmation.
- **Media** — an ISO, the DVD drive and boot order.
- **Debugger** — COM1 serial, the kd session and the symbols it resolves.
- **Guest** — the credential source, the automation toggles, the guest-only BCD toggles and one command.
- **Lab** — diagnostics, package staging, verification, the cycle that runs a package, receipts with export, and the crash triage.
- **Files** — the host's four roots beside the guest's own disk.

Along the bottom of the window is the **log dock**: the unified stream from `logs_subscribe`, live, with a filter for the VM, one for the source (`worker`, `kd`, `serial`, `ssh`, `wmi`, `lab`, `service`), one for the minimum level, a search box, **Pause** and **Clear**. An operation's correlation id is drawn on the first line of its group and left blank on the rest, so a run reads as one run rather than as unrelated lines that happen to be adjacent. The dock keeps the tail it can draw — the oldest line leaves first — and stops asking while it is paused, keeping its offset so nothing is missed and nothing is drawn twice. It is the one place where the AI's work and the operator's clicks appear in the same column.

Closing the window does not stop a VM or guarantee that an already-running guest command was cancelled, and changing pages does not cancel a background operation.

## What the window opens and what it does not rebuild

This is not a second Hyper-V Manager. Creating a VM and editing its hardware are rare, and the manager already does them well, so **Hyper-V Manager** on the fleet opens `mmc.exe virtmgmt.msc` and the window stops there. The console page carries the same idea: the display is a picture, so **Open VMConnect** starts `vmconnect.exe localhost "<vm>"` for full fidelity instead of telling the operator to type that command somewhere else.

Both are the window's own methods rather than service calls, because the service is a background process and a window it started would belong to that process's desktop rather than the operator's. The decision is still Python's, not the page's: `open_vmconnect` asks the service for `list_vms` first and refuses a name the host did not report, which is the allowlist standing between a name and a process. The VM name goes in as one argument, so a VM called `Lab VM` is still one argument and no shell ever parses it. A host without the management tools gets an answer that says so rather than a traceback. The WebView2 fleet row and the classic tkinter window's Overview page both carry the action, next to **Start**.

## Two palettes, one set of names

Every colour is a variable on `:root`, and the light theme is a second block that redefines the same names — not a second stylesheet. That is a rule with a test behind it (`tests/test_webui_page.py`): a name the light block forgets is a colour that stays dark on a light page, and a hex written straight into a rule is a colour the light theme can never reach. The only literal allowed outside the two blocks is `#000` for the guest's own screen, because that is a picture of a display rather than a surface of this window.

The page follows the system until it is told otherwise. **theme** at the foot of the sidebar switches and remembers the choice in the window's own `localStorage`; it never reaches the service, because how the window looks is not a fact about the host. `color-scheme` is set per theme too, so scrollbars and form controls follow instead of staying dark on a light page.

## The keyboard

Every control the script builds is reachable without a pointer. Tabs are two real buttons — the name opens and **×** closes, both with `aria-label` — the nav marks the current page with `aria-current`, and a receipt row is a `role="button"` with a tab stop and an Enter/Space handler, so a table that responds to a click does not become a control only a mouse can reach. The console image is `role="application"` and takes focus, because typing into the guest is what it is for. Focus is drawn everywhere with `:focus-visible`, including on the console, which otherwise removes the browser outline so the image does not shift.

`tests/test_webui_page.py` scans the script for `el("div", {… onclick …})` and reports any element that is not an interactive tag and does not declare a role, a tab stop and a key handler together. That is the check that stops the page rotting into pointer-only as it grows.

A narrow window is a layout, not a scrollbar: below 1100px the grid collapses to one column and cards holding wide tables scroll inside themselves rather than pushing the page sideways. Sizes are in `rem` against one `font-size` on `:root`, so the page follows the window's own scaling rather than being laid out against a hard-coded pixel width.

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

Everything else the window offers, the AI can do too, through the tool of the same name. That parity is enforced in both directions by `tests/test_webui_contract.py`: it parses the page for the method names it calls and fails if one is missing from `HyperVService` or `Api`, and it reads `server.py` for every `service.<method>` an MCP tool reaches and fails if an operation the window offers is in neither that set nor a batch the AI sends as one call. The second direction is the one that catches drift — a method the page grew and no tool ever wrapped. It found one: the window asked `kd_symbols` for the per-driver badge in the receipts table, and no tool asked it, so `kd_inspect` grew `what="symbols"` with the module it is about rather than a tenth kd tool.

The five per-action console calls are the one exception, and it is checked rather than asserted: `console_click`, `console_mouse_move`, `console_press_keys`, `console_type_text` and `console_ctrl_alt_delete` are what `console_batch` carries as its typed actions, and the test reads the discriminator's literals to prove the mapping still names real actions. A click is one call for a person and part of one approval for the AI, and both end at the same `console_input`.

## The console is an image, not video

The console page renders `console_screenshot(source="thumbnail")` a few frames per second and sends clicks as guest-display pixels, mapping the `<img>`'s CSS box back through the `source_width`/`source_height` in the screenshot metadata. `capture_scale` and `coordinates_match_mouse` say whether that mapping is honest, and the page repeats the answer instead of guessing. Typing is batched into one `console_input` call, and a paste box covers OOBE, where the guest has no clipboard to share. **Open VMConnect** on the same card starts the real viewer: this is a picture of the display, not a video stream, and it cannot show a session the VMConnect desktop is drawing exclusively.

Capture stops for a reason: the loop pauses when the window is hidden or the operator paused it, and it *stops asking* — rather than retrying twice a second — when the service refuses, which is what an Off VM or a level without `control` looks like. `docs/PERFORMANCE.md` records what that costs and what it saves.

## Recording a run

A cycle can carry a GIF of the console. Tick **Record the console** (or pass `record=true` to `lab_cycle`/`lab_matrix`) and the service samples the guest's display every two seconds while the cycle runs, writing one animated GIF into the run's own directory and naming it in `lab-cycle.json`. It is evidence, so it is treated like the rest: listed beside the collected files by `lab_receipts(action="get")` with its host path and SHA-256, and compared between runs by presence rather than by hash, because two recordings of the same package never match byte for byte.

The recorder has a budget in frames and in bytes, and it **thins rather than stops** when it reaches it: past sixty frames it keeps every other one and samples half as often, so a long run is still covered end to end and the GIF still ends on the screen the run stopped on. If the frames then exceed the byte budget the writer drops more, and the report says how many were sampled, how many were kept and whether the set was truncated. A frame that cannot be read is one frame fewer, never an error: a recording must not fail the cycle that produced it.

## The guest transport

The Overview page answers two different questions and does not let one stand in for the other. **Which path is configured** comes from the settings: a per-VM `HYPERV_MCP_GUEST_TRANSPORT_<VM>` variable first, then the operator's saved choice, then the host-wide default. **Whether it answers** comes from one trivial command (`$env:COMPUTERNAME`) sent over the transport the next call would actually use, timed. A green light means the guest replied just now and the number beside it is that round trip; a red one means it did not, with the error code and the message. A failed probe is a state, not an exception: the page has to be able to draw a red light.

The switch below it is `guest_transport_set`, which saves the choice to `%LOCALAPPDATA%\HyperVConsoleMCP\transport.json` and applies it to the next call without a restart. `auto` is the absence of a preference rather than a third saved value, so it removes the record: the file says what the operator decided, and "let the probe decide" is a decision to stop deciding. When an environment variable is setting the transport, the answer says so, because the saved choice is then not what wins. SSH is reported on its own line as well — provisioned, answering, latency — so a VM carried by PowerShell Direct can still show a healthy sshd, or the reason it is not.

## The matrix

The **Matrix** page runs the same package through the same cycle on two to four VMs, one after another, and puts the outcomes side by side. It is one call (`lab_matrix`) and one approval: a single click that runs a payload four times has to say so, and the approval summary names every VM and its per-VM run id. Each VM gets `<run_id>-<vm>`, slugged into a directory name, so the receipts never collide.

The runs are sequential, not concurrent, because each cycle owns the guest worker, a checkpoint, a debugger attachment and the console for its whole length. One VM refusing the run — off, out of level, no guest — becomes that VM's row with its error code, and the matrix continues: the other rows are still the answer. A failure that is not about the VM is not swallowed.

The result is two tables. The first is one row per VM: outcome, duration, bugcheck, blamed driver, whether the symbols loaded, the recording. The second is one row per compared field with a column per VM, so "identical everywhere except the bugcheck on Windows 11" is a highlighted row rather than a reading exercise. The comparing is the service's (`lab.matrix.compare`); the page only draws it, because a page that decided what "same" meant would be a second implementation of it. Progress arrives as the `lab_matrix` events, each band labelled with the VM that is running.

## The file manager

Two panels, one per side. The host panel is limited to the four roots this server was configured with — `HYPERV_MCP_LAB_ROOT`, `HYPERV_MCP_EVIDENCE_ROOT`, `HYPERV_MCP_TRANSFER_ROOT` and `HYPERV_MCP_ISO_ROOT` — and never to `C:\`: a path is resolved and then compared with the resolved root, so `..` and an absolute path elsewhere fail the same check. The guest panel reaches the lab machine's own disk through the configured transport.

Both sides list, create a folder, rename, delete and copy to the other side. Copying a file either way is `guest_put_file`/`guest_get_file`, so the SHA-256 is verified at the destination and the digest shown is the one the far side computed; an upload crosses the VMBus with `Copy-VMFile` when the guest's Guest Service Interface is enabled and the guest session otherwise, and the page does not care which, because both answer with the guest's own digest. A file row can also be **dragged onto the other panel**, which is the same copy: the drop calls the row's own path rather than a second transfer, and a drop back on the panel it came from is not a move and does nothing. Directories do not drag, because the service copies one file at a time and a folder would need a recursive transfer it does not offer. The button stays beside the drag, because a drag is the one gesture a keyboard cannot make. Deleting takes two clicks on the same row and the service still requires the path spelled back; a non-empty directory additionally needs the "delete folders recursively" box, and the guest script refuses the tree without it too. Progress comes from the event bus: the bar reads the `progress` events the transfer publishes, so it shows the same numbers on the Activity page.

## The lab page

The lab page is built around one rule: a cycle is a single service call, so the page cannot know how far it got by watching a spinner. It reads the `started`/`progress`/`completed` events the service publishes for `lab_cycle` and `lab_run` — the same records the Activity page shows, under one correlation id — and draws the newest phase with its percentage. The numbers are the service's, not the page's estimate.

Running a package is offered only in the lab profile, because that is where the server accepts it. Staging (`lab_stage`) is a host-to-guest copy of a directory under `HYPERV_MCP_LAB_ROOT` with a manifest built on both sides; `lab_verify` compares the published package with an approved digest; `lab_collect` reads evidence without executing anything. The wave in the roadmap called this pair `lab_plan`/`lab_apply`; the service has no such methods and the page does not invent them, so the staging panel calls what exists.

**Receipts** are the read side of `lab-cycle.json`. `lab_receipts(action="list")` names every run under the evidence root, newest first and optionally for one VM; a directory holding no readable record is listed as skipped rather than guessed at. The table carries a **symbols** badge per run — whether the driver's `.pdb` resolved, which is what makes a stack readable — and a console recording shows in the Files column like any other piece of evidence. Clicking a row opens it: the record's fields, and every evidence file with its host path and SHA-256, each with a jump into the file manager. Clicking two rows and then "Compare" runs `lab_receipts(action="compare")`, which reports what matched and what changed, and marks a field one side never had as `absent` — a run that crashed has a blamed driver and a run that passed does not, and that is not the same as "changed to nothing". **Export all** is `lab_receipts(action="export")`: it copies every receipt, optionally for one VM, into one JSON document under `exports/` beneath the evidence root, with each record's hash, and leaves the per-run records where they are. The export directory is not itself read back as a run.

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
| 5 | Multi-VM matrix with the side-by-side comparison, console recording attached to the receipt, the per-VM transport light and switch, the symbols badge and receipt export. |

Each wave was closed with the whole suite green — pytest, ruff, the PowerShell parse, `Invoke-ScriptAnalyzer`, the stdio smoke test and `node --check` on the page's script — before it was committed.

The round that closed the roadmap's global definition of done added no page. It added the second palette and the keyboard paths described above, the parity check in the direction that catches drift, and `tests/test_webui_fake_service.py`: a fake that answers every name the page calls with the name and the arguments it was given, so the page's whole contract is exercised with no WebView, no Hyper-V and no guest. What that fake proves is that the list of names is answerable and that a name the service does not have, or one called with the wrong arguments, comes back as a refusal the page can draw instead of a traceback that blanks the window. What it cannot prove is that the real service returns the shapes the page draws — that is what the service's own tests are for, and what a run against a real host is for.

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
