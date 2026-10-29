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

A schema-1 manifest lists each relative POSIX path, byte length and lowercase SHA-256. The receipt's `manifest_sha256` hashes canonical JSON (UTF-8, sorted keys, compact separators). PowerShell and Python use the same canonical representation.

Limits are 512 files and 2 GiB per package. Paths must be portable Windows relative paths, ASCII and at most 240 characters. Absolute paths, traversal, reserved device names, invalid characters, trailing dots/spaces and case-insensitive duplicates are rejected. Symbolic links are rejected while building a package.

The host rechecks every file before sending it. Small files use atomic guest writes; large files use 1–8 MiB chunks (4 MiB by default), temporary parts, assembly into a temporary destination and final size/hash verification. Package IDs and run IDs allow letters, digits, dots and hyphens, with a 64-character limit. A package cannot be overwritten implicitly.

Staging is all-or-nothing. Every file, plus the manifest, is written under `C:\DriverLab\Transfers\<transfer-id>\<package-id>` and re-verified in the guest — size, SHA-256, the manifest hash the host approved, and the absence of unlisted files. Only then is that one directory renamed into `C:\DriverLab\Packages`. A transfer that fails part-way leaves `Packages` untouched, and `init` deletes staging directories older than 24 hours.

`Test-DriverLabPackage.ps1` fails when the package directory holds a file the manifest does not describe, and the run entry point must be a listed manifest member (`Run-Lab.ps1`), not merely a file that happens to exist on disk.

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

## Autonomous runs (lab profile only)

With `HYPERV_MCP_PROFILE=lab`, `lab_run` performs the same deliberate sequence without a human click:

1. read `vm_get` and refuse unless the VM is already `Running`;
2. create checkpoint `pre-<run_id>`;
3. re-verify the package and its manifest SHA-256 in the guest;
4. run `Invoke-DriverLabRun.ps1` with the approval phrase `EXECUTE <package_id> <run_id>` generated by the server itself;
5. read `C:\DriverLab\Runs\<run_id>\result.json` and classify the outcome.

`lab_cycle` does the same and then collects evidence (kernel dump included by default) and, with `revert_after=true`, restores `pre-<run_id>`.

`outcome` is one of:

| Outcome | Meaning |
|---|---|
| `ok` | the guest runner finished and reported success |
| `failed` | the runner finished non-zero, or answered without writing a result |
| `not_verified` | the package did not match its manifest; nothing was executed |
| `bugcheck` | the guest stopped answering and `vm_get` shows a lower uptime, i.e. a reboot |
| `hang` | the guest stopped answering while the VM kept running |
| `timeout` | the run hit `timeout_seconds` (10–3600) with the guest still alive |

A `bugcheck` report also carries the System log's BugCheck (event 1001) entries when the guest came back far enough to answer. `vm_inject_nmi` forces a hung guest into the debugger when a hang must be turned into a crash to get a dump.

The run is synchronous: the guest worker is busy for its duration, while `vm_get`, console and dashboard calls keep working on the host worker. `lab_run` and `lab_cycle` are refused outside the lab profile — the approval phrase is never typed by the caller, and the profile is the operator's decision to let a loop execute staged packages.
