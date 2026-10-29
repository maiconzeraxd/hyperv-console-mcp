# Reproducible driver lab

The lab separates **staging**, **verification**, **manual execution** and **evidence collection**. Initialization, staging, verification and collection never execute a staged payload. No driver, mapper or `Run-Lab.ps1` is bundled.

Use a disposable, checkpointed Windows VM. Do not run a BYOVD mapper or test driver on the host. Loading any such payload requires a separate, deliberate operator decision immediately before that run; dashboard approval of staging is not execution approval.

## Guest layout

Initialization installs the packaged tools and creates only this fixed layout:

```text
C:\DriverLab\Packages
C:\DriverLab\Transfers
C:\DriverLab\Runs
C:\DriverLab\Evidence
C:\DriverLab\Tools
```

It does not change Secure Boot, VBS/HVCI, test-signing, BCD, networking or crash-dump settings. Existing tools are accepted only when their SHA-256 matches the packaged version; divergent tools are not silently overwritten.

## Host roots

MCP `lab_stage` accepts only a subdirectory of `HYPERV_MCP_LAB_ROOT`, defaulting to:

```text
%LOCALAPPDATA%\HyperVConsoleMCP\lab\packages
```

Do not put files directly in that root; create one directory per package. Evidence is downloaded below `HYPERV_MCP_EVIDENCE_ROOT`, defaulting to:

```text
%LOCALAPPDATA%\HyperVConsoleMCP\evidence
```

Both roots, plus `HYPERV_MCP_EVENT_DIRECTORY` (default `%LOCALAPPDATA%\HyperVConsoleMCP\events`), are read into `Settings` when the server starts and must be absolute paths; a relative value fails startup instead of being resolved against the current directory.

## CLI workflow

Set the protected environment described in [SETUP.md](SETUP.md), including PowerShell Direct credentials, then run:

```powershell
hyperv-lab --vm NORTH-Test init
hyperv-lab --vm NORTH-Test diagnostics
hyperv-lab --vm NORTH-Test stage C:\Users\you\AppData\Local\HyperVConsoleMCP\lab\packages\example --package-id example.1 --receipt .\stage-receipt.json
hyperv-lab --vm NORTH-Test verify example.1 <manifest-sha256-from-receipt>
hyperv-lab --vm NORTH-Test checkpoint before-example-1
hyperv-lab --vm NORTH-Test collect example.1 run-1 .\evidence
```

The dashboard exposes the same steps. `lab_diagnostics` reports boot, Secure Boot, VBS/HVCI, test-signing, debugger and crash-dump state without modifying it. `checkpoint` uses the ordinary checkpoint policy and creates VM state; it is not a read-only command.

## Manifest and transfer integrity

A schema-1 manifest lists each relative POSIX path, byte length and lowercase SHA-256. The receipt's `manifest_sha256` hashes canonical JSON (UTF-8, sorted keys, compact separators), and that hash is always computed in Python, over the bytes the host wrote. Nothing in the guest re-serializes a manifest: a second canonicalizer is a second opinion about the same bytes, and the two disagreeing was a real defect class.

Limits are 512 files and 2 GiB per package. Paths must be portable Windows relative paths, ASCII and at most 240 characters. Absolute paths, traversal, reserved device names, invalid characters, trailing dots/spaces and case-insensitive duplicates are rejected. Symbolic links are rejected while building a package.

The host rechecks every file before sending it. Small files use atomic guest writes; large files use 1–8 MiB chunks (4 MiB by default), temporary parts, assembly into a temporary destination and final size/hash verification. Package IDs and run IDs allow letters, digits, dots and hyphens, with a 64-character limit. A package cannot be overwritten implicitly.

Staging is all-or-nothing. Every file, plus the manifest, is written under `C:\DriverLab\Transfers\<transfer-id>\<package-id>`; the guest then reports the manifest bytes it holds and a listing of every file with its size and SHA-256, and `lab/publish.py` decides on the host whether that is the package that was approved. Only then is that one directory renamed into `C:\DriverLab\Packages`. A transfer that fails part-way leaves `Packages` untouched, and `init` deletes staging directories older than 24 hours.

`lab_verify` works the same way on a published package: the guest lists `C:\DriverLab\Packages\<package-id>`, and the host compares the manifest bytes and every file with the approved digest. The guest never returns a verdict, so a refusal names the file that failed and can be reproduced in a unit test without a VM.

`Test-DriverLabPackage.ps1` remains as the in-guest tripwire the manual runner calls immediately before executing `Run-Lab.ps1`: it hashes the manifest file's bytes, checks every listed file and fails when the directory holds a file the manifest does not describe. The run entry point must be a listed manifest member (`Run-Lab.ps1`), not merely a file that happens to exist on disk.

Evidence downloads validate each chunk and the final SHA-256, write to a unique temporary host file, flush it and rename only after verification. Evidence collection is not a rollback and does not remove guest artifacts.

## Manual run boundary

After reviewing the staged package and creating a checkpoint, an elevated **guest** operator may deliberately invoke:

```powershell
& 'C:\DriverLab\Tools\Invoke-DriverLabRun.ps1' `
  -PackageId 'example.1' `
  -RunId 'run-1' `
  -ExpectedManifestSha256 '<manifest-sha256>' `
  -ApproveExecution 'EXECUTE example.1 run-1'
```

The runner checks administrator and hypervisor presence, the exact approval phrase, the manifest hash and every file hash. The package itself must supply `Run-Lab.ps1`. This generic project does not supply that entry point and does not decide what a package executes.

A checkpoint, debugger attachment or hash match does not make arbitrary kernel code safe and does not prevent a bugcheck. Preserve evidence before retrying with a different run ID.

## Preparing the guest

`lab_guest_configure` applies the boot and crash settings a driver lab needs, and records each one in the same `guest-config.json`:

| Setting | Effect |
|---|---|
| `testsigning=true` | `bcdedit /set {current} testsigning on` |
| `kernel_debug=true` | `bcdedit /set {current} debug on`; the transport (serial or KDNET) is configured separately |
| `crash_dump=kernel\|complete\|none` | `CrashDumpEnabled`, `AutoReboot`, `Overwrite`, `AlwaysKeepMemoryDump` under `CrashControl` |
| `disable_hvci=true` | `EnableVirtualizationBasedSecurity=0` and HVCI `Enabled=0` |

Every toggle is tri-state: `true` applies it, `false` restores the value it replaced, and omitting it leaves that setting alone. All of them need a reboot, which `restart=true` performs (graceful shutdown, start, wait for an interactive session) when `HYPERV_MCP_ALLOW_POWER` is on.

The tool reads `Get-VMFirmware` first. If Secure Boot is On, test signing and kernel debugging would be ignored, so it refuses; pass `secure_boot_off=true` **with the VM Off** and it turns Secure Boot off as its only action, then tells you to start the VM and call it again. It never changes power state on its own.

`guest_config_status` reports the live values under `driver_ready`, including the crash-dump mode and whether the pagefile is large enough for a complete dump (RAM plus about 257 MB). The pagefile is reported, not resized.

## Autonomous runs (lab profile only)

With `HYPERV_MCP_PROFILE=lab`, `lab_run` performs the same deliberate sequence without a human click:

1. read `vm_get` and refuse unless the VM is already `Running`;
2. create checkpoint `pre-<run_id>`;
3. re-verify the published package on the host, from the guest's listing of it;
4. run `Invoke-DriverLabRun.ps1` with the approval phrase `EXECUTE <package_id> <run_id>` generated by the server itself;
5. read `C:\DriverLab\Runs\<run_id>\result.json` and classify the outcome.

`lab_cycle` does the same and then collects evidence (kernel dump included by default) and, with `revert_after=true`, restores `pre-<run_id>`.

With `debugger="kdnet"` or `"serial"` the cycle also attaches kd before step 4 and, because it can read `lab-manifest.json` from the guest, arms `sxe ld:<driver>` for every `.sys` the package ships before the payload can load one. The report's `debugger.break_on_load` says which modules were found and which were armed (`error` is set if a break could not be armed, and the cycle carries on). A package with no driver, or an unreadable manifest, simply arms nothing.

`outcome` is one of:

| Outcome | Meaning |
|---|---|
| `ok` | the guest runner finished and reported success |
| `failed` | the runner finished non-zero, or answered without writing a result |
| `not_verified` | the package did not match its manifest; nothing was executed |
| `bugcheck` | the host recorded a guest crash during the run (event 18590), or the guest stopped answering and the VM rebooted |
| `hang` | the guest stopped answering while the VM kept running |
| `timeout` | the run hit `timeout_seconds` (10–3600) with the guest still alive |

A `bugcheck` report is triaged rather than left at the code: `lab/crash.py` picks the dump the cycle
collected — the newest file under `minidumps/`, falling back to `MEMORY.DMP` — runs `!analyze -v`
against it with kd on the host, and merges the reading with the host's own event-18590 record into
`report.crash`. The merge is what keeps a run honest when kd cannot open the dump or is not
installed: the host's bugcheck code and parameters stay, and `crash.error` says why. The full kd
output is kept beside the evidence as `kd-analyze.log`. `crash.bugcheck.symbols_resolved` says
whether the names in the stack are real; when a debugger was attached, `debugger.symbols` names each
driver the package ships as resolved or not, which is the difference between a stack with function
names and a module plus an offset. `vm_inject_nmi` forces a hung guest into the debugger when a hang
must be turned into a crash to get a dump.

Every cycle also writes `lab-cycle.json` beside the evidence: the package and the manifest hash the
run was asked to match, the checkpoint and its revert, the outcome, the crash reading, the symbol
result, and one hash over the collected files. Two runs of the same package are a file diff.

## Driver Verifier and the driver service

`lab_verifier` turns Driver Verifier on for named test drivers or for every driver, turns it off, or
restores what the tool replaced. The flags are read from and written to the registry
(`VerifyDriverLevel`, `VerifyDrivers`), never from `verifier /query`, whose output is translated on a
localized guest. Enabling records the previous setting in `C:\DriverLab\State\verifier.json`;
`revert` restores it and removes the file, so an operator's own Verifier configuration survives the
lab. A change takes effect on the next restart, and this tool never restarts the VM.
`lab_diagnostics` reports the current state.

`lab_driver_service` installs a kernel-driver service for a `.sys` already staged under
`C:\DriverLab`, starts, stops and removes it, or lists what this tool installed. It drives the
Service Control Manager through advapi32, so `StartService`'s Win32 error and the driver's own
NTSTATUS (from `SERVICE_STATUS.dwServiceSpecificExitCode`) are reported as numbers. Signature
enforcement is never bypassed: a driver the guest refuses is reported with its code, and test signing
stays the operator's boot setting (`lab_guest_configure`). Reading the state needs no confirmation;
the other actions require `confirm_vm`.

The run is synchronous: the guest worker is busy for its duration, while `vm_get`, console and dashboard calls keep working on the host worker. `lab_run` and `lab_cycle` are refused outside the lab profile — the approval phrase is never typed by the caller, and the profile is the operator's decision to let a loop execute staged packages.
