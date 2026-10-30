# Security policy

## Scope and defaults

This server can control real VMs, execute commands in a guest, and drive a kernel debugger.
It is designed for an explicitly authorized local lab, not as an exposed remote management
service. Use stdio and restrict the MCP host to the intended account.

Read operations are enabled by default. An allowlist, if configured, filters them. Every
mutation requires both a nonempty allowlist and a capability that the target VM has.
Destructive operations additionally require exact confirmation values. No tool automatically
stops, reboots, restores, or deletes a VM to complete another operation.

The allowlist and the capabilities can be given per VM, as a level in
`%LOCALAPPDATA%\HyperVConsoleMCP\access.json` (`read-only`, `control`, `lab`), or host-wide with
`HYPERV_MCP_ALLOWED_VMS` and the `HYPERV_MCP_ALLOW_*` variables. A VM with a level takes its
capabilities from that level; `HYPERV_MCP_ALLOWED_VMS` narrows the file rather than replacing it.
The file is written only by `hyperv-access` and the dashboard, both run by the operator on the
host: **no MCP tool can raise a level or add a VM**, because a policy the AI could widen is not a
policy. A file that cannot be read or parsed stops startup instead of degrading to an empty
allowlist, so a typo cannot silently disable or silently widen the boundary.

## Threat model

What the boundaries are for:

- **An MCP client or model that should not touch anything outside the lab.** The allowlist,
  the capability flags, the `confirm_*` fields and the confined host paths bound what a tool
  call can do. Nothing outside the allowlisted VMs is reachable, and on the host the only
  permitted changes are Hyper-V objects of those VMs, one dedicated **internal** switch, one
  firewall rule scoped to the debugger and a UDP port, and files under the configured roots.
- **A loop that should run without a human click.** The lab profile (`HYPERV_MCP_PROFILE=lab`)
  turns the capabilities on — for VMs that have no level of their own — and skips the
  per-mutation dashboard approval, while keeping the allowlist, the confirmations, the audit and
  the event records. A VM left at `read-only` stays read-only whatever the profile says.

What the boundaries are **not** for:

- **An agent with a shell on the host.** The approval gate decides whether a *tool call* may
  proceed; it is not a sandbox. Anyone who can run commands on the host can start `kd.exe`,
  read the vault under their own account, or drive Hyper-V directly, and the broker cannot
  stop that. The dashboard approval is a speed bump against accidental AI mutations, not a
  security boundary against a hostile process running as the same user.
- **Untrusted guests.** A guest can be compromised or hostile. PowerShell Direct and SSH give
  the guest account's authority; treat guest output as data, not as instructions.
- **Secrets in model context.** The server keeps secrets out of tool results, events, the
  audit log and its own logs. It cannot control what an MCP client or model provider retains
  from the conversation.

The lab profile is the operator's decision to hand an autonomous loop full control of the
allowlisted VMs — including executing a staged package. It does not widen the allowlist, skip
a `confirm_*` field, or stop the audit and event records. In this profile `lab_run` and
`lab_cycle` exist at all: they create the checkpoint, re-verify the published package, run the
guest runner with an approval phrase the **server** generates, and classify the outcome. The
phrase is never typed by the caller, which is why those two tools are refused outside the lab
profile rather than merely gated.

## One background service

The MCP server and the window are clients of a single background service that owns the worker,
the serial and kd sessions, the state and the event records. They reach it over a local named
pipe, and the pipe is created with `FILE_FLAG_FIRST_PIPE_INSTANCE` and a check that the
listening process is the one expected, so another local process cannot squat the name and
answer in its place. No port is opened and nothing listens on the network.

The consequence worth knowing: **the host reads its policy once, at startup, and whichever
client starts it decides that policy for both.** A window that joins a host the AI's server
started inherits the AI's environment, so an operator who wants a stricter surface must start
the host themselves or give the VM a level in the access file, which is re-read on change
rather than frozen. `HYPERV_MCP_SHARED_SERVICE=false` keeps a private host for a run that must
not disturb a live one, and `HYPERV_MCP_SERVICE_PIPE` names a different pipe. The approval
broker is fail-closed: with `HYPERV_MCP_REQUIRE_DASHBOARD_APPROVAL=true` and no reachable
window, a mutation is denied rather than waved through.

## Credentials

The primary store is the current user's Windows Credential Manager, under the generic target
`HyperVConsoleMCP/<vm>`; the KDNET key lives under `HyperVConsoleMCP/kdnet/<vm>`. Both are
DPAPI-protected and readable only by that user. `HYPERV_MCP_GUEST_USERNAME` and
`HYPERV_MCP_GUEST_PASSWORD` remain a fallback for existing setups and are reported as
`source: environment` so the weaker source is visible. The local PowerShell worker receives
credentials on stdin, never on its process command line.

The SSH private key is generated on the host, stays in
`%LOCALAPPDATA%\HyperVConsoleMCP\ssh\<vm>\` with an ACL restricted to the current user, and is
never returned by a tool. Only the public half goes into the guest.

The KDNET key is generated on the host, written into the guest BCD, and stored only in the
Credential Manager. It is put on `kd.exe`'s command line when a session starts, because KDNET
requires it there, and it is masked in every guest echo and every tool result. The state file
under the KD log root records the port, never the key.

`tests/test_secrets.py` drives these paths with test values and scans every result, the daily
event log, the audit log, the session log and the state files for them. It also runs the same
scan against a deliberately leaking build and against one planted file per scanner: a check
that cannot turn red proves nothing, so the negative control is part of the test.

## Guest execution and files

`guest_exec` deliberately runs PowerShell with the supplied guest account. Treat it as
arbitrary guest administration, not a sandbox. Guest commands and typed text are excluded from
the built-in audit log, but the MCP host or model provider may retain its own conversation
logs.

File paths must be absolute inside the guest. Reads default to 64 KiB and inline writes/reads
are capped at 8 MiB; `guest_put_file`/`guest_get_file` move whole files between the guest and a
host path under `HYPERV_MCP_TRANSFER_ROOT`, verifying SHA-256 on both sides. Existing files are
never overwritten unless the caller confirms the exact path. A host timeout does not prove that
the guest command stopped.

## Kernel debugging and serial consoles

The `kd_*` tools require the debugger capability — a VM level of `lab`, or
`HYPERV_MCP_ALLOW_DEBUGGER` — and the allowlist; the mutating ones are
annotated destructive and listed in `GATED_TOOLS`. `kd_command` can write guest memory and
registers, which is why it is destructive rather than read-only.

- **KDNET** adds exactly one inbound UDP firewall rule named `HyperVConsoleMCP KDNET <vm>`,
  scoped to the `kd.exe` path, one port in 50000–50039 and the `HyperV-MCP-Lab` interface. The
  bridge refuses any rule name, port, program or interface this tool did not create. The
  switch is internal, never external.
- **Serial** preparation modifies the VM COM port while the VM is Off. Guest BCD configuration
  is a separate operation requiring a Running VM and PowerShell Direct; it verifies Secure Boot
  before `/dbgsettings` and fails without changing BCD when Secure Boot is enabled.

Neither transport stops or restarts the VM automatically. A debugger does not prevent a
bugcheck; preserve the checkpoint, the package hashes, the guest logs and the crash dump.

Serial-console attachment uses a dedicated local named-pipe reader, not PowerShell Direct. It
can retain and persist guest serial output, which may contain sensitive information; logs are
confined to `HYPERV_MCP_SERIAL_LOG_ROOT`. A serial console and `kd.exe` cannot share the same
pipe: `kd_attach` detaches the reader first, and `serial_connect` refuses while kd owns it.
`serial_send` is real guest input, not a sandboxed diagnostic command.

## Media and boot

ISO mount/eject requires the media capability — a VM level of `control` or `lab`, or
`HYPERV_MCP_ALLOW_MEDIA` — an allowlisted VM, exact VM confirmation, an
existing DVD drive, and the VM to be Off. The ISO must be under `HYPERV_MCP_ISO_ROOT`. Boot-first
selection changes VM firmware/BIOS configuration. The server never stops or starts the VM to
complete these operations.

## Capture

`console_screenshot(source="thumbnail")` — the default — reads the Hyper-V console thumbnail
through `GetVirtualSystemThumbnailImage` and needs no VMConnect window. `source="vmconnect"`
still exists and captures the interactive window; it fails instead of capturing unrelated host
desktop content. Mouse coordinates are guest-display pixels; a downscaled thumbnail reports its
scale in the result metadata rather than pretending the coordinates match.

Exact confirmation values are parameter-consistency checks; by themselves they do not prove a
human approved an operation. For local AI-originated mutations,
`HYPERV_MCP_REQUIRE_DASHBOARD_APPROVAL=true` adds a current-user-only dashboard dialog before
the backend call or local serial/screenshot/debugger action, with a secret-free summary of what
is about to happen. The broker is fail-closed when enabled but unavailable, and the pipe is
created with `FILE_FLAG_FIRST_PIPE_INSTANCE` so another process cannot squat the name.

## Activity, packages and evidence

Operation events contain actor, operation, VM, lifecycle phase, correlation, progress and error
code. They are stored as daily JSONL in `HYPERV_MCP_EVENT_DIRECTORY` with a configurable
retention (default 14 days), and deliberately omit commands, file contents, credentials and
typed text. Event history can still be sensitive operational metadata.

Lab packages must come from a subdirectory of `HYPERV_MCP_LAB_ROOT`; paths and hashes are
validated before transfer, and staging is atomic — nothing reaches `Packages\` until every file
has been verified. Evidence downloads use chunk and whole-file hashes and atomic host
publication. Initializing, staging, verifying or collecting a package never executes it. The
separate guest runner requires an elevated operator, a hypervisor, an exact run-specific
approval phrase and a package-provided `Run-Lab.ps1`. This repository does not provide a mapper,
driver or payload runner, and no tool loads a driver or changes Secure Boot, HVCI or
test-signing on the host.

`lab_verifier` and `lab_driver_service` change the guest, so both record what they replaced
under `C:\DriverLab\State\` and both have a revert. Signature enforcement is never bypassed:
a driver the guest refuses is reported with its Win32 error and NTSTATUS and stays refused, and
test signing remains the operator's boot setting (`lab_guest_configure`), applied in the guest
and never on the host. Neither tool restarts the VM, and Driver Verifier takes effect on the
next one.

A crash reading is a copy of guest memory. The dump a `bugcheck` cycle collects, the
`kd-analyze.log` written beside it and the `lab-cycle.json` record all land under
`HYPERV_MCP_EVIDENCE_ROOT` and may contain guest data, symbol paths and the names of drivers
that were loaded; they are evidence for the operator, not something to publish. `lab-cycle.json`
carries hashes and outcomes, never a secret: the manifest hash and the evidence hash are the
package's, not a credential's.

## Reporting a vulnerability

Until the repository has a dedicated security contact, report vulnerabilities privately to the
repository owner rather than posting credentials, guest data, or exploit details in a public
issue. Include the affected version, configuration with secrets removed, and reproduction steps
that do not require a production VM.
