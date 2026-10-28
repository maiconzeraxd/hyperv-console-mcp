# Security policy

## Scope and defaults

This server can control real VMs and execute commands in a guest. It is designed for an explicitly authorized local lab, not as an exposed remote management service. Use stdio and restrict the MCP host to the intended account.

Read operations are enabled by default. An allowlist, if configured, filters them. Every mutation requires both a nonempty `HYPERV_MCP_ALLOWED_VMS` and a capability-specific opt-in. Destructive operations additionally require exact confirmation values. No tool automatically stops, reboots, restores, or deletes a VM to complete another operation.

## Credentials

Set `HYPERV_MCP_GUEST_USERNAME` and `HYPERV_MCP_GUEST_PASSWORD` only in a protected MCP-host environment. Do not commit them or place them in tool calls. The local PowerShell worker receives credentials on stdin, never on its process command line. This is not a vault: anyone who can read the server environment or local process may still obtain them. Use a disposable VM account with only the permissions required by the lab.

## Guest execution and files

`guest_exec` deliberately runs PowerShell with the supplied guest account. Treat it as arbitrary guest administration, not a sandbox. Guest commands and typed text are excluded from the built-in audit log, but the MCP host or model provider may retain its own conversation logs.

File paths must be absolute inside the guest. Reads default to 64 KiB and writes/reads are capped at 8 MiB; increase read limits deliberately instead of sending large files into model context. Writes use a temporary file followed by replacement. Existing files are never overwritten unless the caller confirms the exact path. A host timeout does not prove that the guest command stopped.

## Kernel debugging and serial consoles

Serial debug preparation modifies the VM COM port while the VM is Off. Guest BCD configuration is a separate operation requiring a Running VM and PowerShell Direct. It verifies Secure Boot before `/dbgsettings`; when Secure Boot is enabled, it fails before changing BCD. Both operations are gated independently and never automatically stop or restart the VM. KDNET configuration, network attachment, firewall modification, and guest key generation are not automatic. Keep debugger keys private and isolate driver-test VMs from untrusted networks.

Serial-console attachment uses a dedicated local named-pipe reader, not PowerShell Direct. It can retain and persist guest serial output, which may contain sensitive information; logs are confined to `HYPERV_MCP_SERIAL_LOG_ROOT` (default `%LOCALAPPDATA%\HyperVConsoleMCP\serial`). Apply normal host access controls to that directory. A serial console and WinDbg cannot share the same pipe attachment. Detach the reader before launching the debugger. `serial_send` is real guest input, not a sandboxed diagnostic command.

## Media and boot

ISO mount/eject requires `HYPERV_MCP_ALLOW_MEDIA`, an allowlisted VM, exact VM confirmation, an existing DVD drive, and the VM to be Off. The ISO must be under `HYPERV_MCP_ISO_ROOT` (default `%LOCALAPPDATA%\HyperVConsoleMCP\media`). Boot-first selection changes VM firmware/BIOS configuration. The server never stops or starts the VM to complete these operations.

## Capture

`console_screenshot` uses the interactive VMConnect window, not a documented guest framebuffer API. It is experimental and separately gated. If direct window rendering is unavailable, the tool fails instead of capturing unrelated host desktop content. Mouse coordinates are guest-display pixels; scaled screenshots and VMConnect chrome are not a reliable coordinate transform.

Exact confirmation values are parameter-consistency checks; by themselves they do not prove a human approved an operation. For local AI-originated mutations, `HYPERV_MCP_REQUIRE_DASHBOARD_APPROVAL=true` adds a current-user-only dashboard dialog before the backend call or local serial/screenshot action. The broker is fail-closed when enabled but unavailable. It does not forward commands, payloads, credentials or typed text, so the operator must review those in the originating conversation. MCP clients without the dashboard retain only the ordinary policy gates.

## Activity, packages and evidence

Operation events contain actor, operation, VM, lifecycle phase, correlation, progress and error code. They are stored in `HYPERV_MCP_EVENT_DIRECTORY` and deliberately omit commands, file contents, credentials and typed text. Event history can still be sensitive operational metadata.

Lab packages must come from a subdirectory of `HYPERV_MCP_LAB_ROOT`; paths and hashes are validated before transfer. Evidence downloads use chunk and whole-file hashes and atomic host publication. Initializing, staging, verifying or collecting a package never executes it. The separate guest runner requires an elevated operator, a hypervisor, an exact run-specific approval phrase and a package-provided `Run-Lab.ps1`. This repository does not provide a mapper, driver or payload runner.

## Reporting a vulnerability

Until the repository has a dedicated security contact, report vulnerabilities privately to the repository owner rather than posting credentials, guest data, or exploit details in a public issue. Include the affected version, configuration with secrets removed, and reproduction steps that do not require a production VM.
