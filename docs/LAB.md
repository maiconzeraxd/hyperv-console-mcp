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
%USERPROFILE%\HyperVLabs\packages
```

Do not put files directly in that root; create one directory per package. Evidence is downloaded below `HYPERV_MCP_EVIDENCE_ROOT`, defaulting to:

```text
%LOCALAPPDATA%\HyperVConsoleMCP\evidence
```

## CLI workflow

Set the protected environment described in [SETUP.md](SETUP.md), including PowerShell Direct credentials, then run:

```powershell
hyperv-lab --vm NORTH-Test init
hyperv-lab --vm NORTH-Test diagnostics
hyperv-lab --vm NORTH-Test stage C:\Users\you\HyperVLabs\packages\example --package-id example.1 --receipt .\stage-receipt.json
hyperv-lab --vm NORTH-Test verify example.1 <manifest-sha256-from-receipt>
hyperv-lab --vm NORTH-Test checkpoint before-example-1
hyperv-lab --vm NORTH-Test collect example.1 run-1 .\evidence
```

The dashboard exposes the same steps. `lab_diagnostics` reports boot, Secure Boot, VBS/HVCI, test-signing, debugger and crash-dump state without modifying it. `checkpoint` uses the ordinary checkpoint policy and creates VM state; it is not a read-only command.

## Manifest and transfer integrity

A schema-1 manifest lists each relative POSIX path, byte length and lowercase SHA-256. The receipt's `manifest_sha256` hashes canonical JSON (UTF-8, sorted keys, compact separators). PowerShell and Python use the same canonical representation.

Limits are 512 files and 2 GiB per package. Paths must be portable Windows relative paths, ASCII and at most 240 characters. Absolute paths, traversal, reserved device names, invalid characters, trailing dots/spaces and case-insensitive duplicates are rejected. Symbolic links are rejected while building a package.

The host rechecks every file before sending it. Small files use atomic guest writes; large files use 1–8 MiB chunks (4 MiB by default), temporary parts, assembly into a temporary destination and final size/hash verification. Package IDs and run IDs allow letters, digits, dots and hyphens, with a 64-character limit. A package cannot be overwritten implicitly.

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
