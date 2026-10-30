# Driver testing: the QA flow

This is the loop the Phase 6 tools exist for: build a test-signed driver, stage it as a package,
run it in a disposable VM under Driver Verifier, read what happened, and keep a record that two
runs can be compared on. It is a workflow for a **driver you built and signed**, in a VM you can
throw away.

What it is not: a loader for someone else's kernel code. No tool here loads a driver, and none
bypasses Driver Signature Enforcement. The package supplies `Run-Lab.ps1`, the guest runner
requires an elevated operator and an exact approval phrase, and the lab profile's `lab_run`
exists only because the operator chose that profile. See [LAB.md](LAB.md) for the boundary and
[SECURITY.md](../SECURITY.md) for the threat model.

## 1. Before the first run

The guest has to be able to load a test-signed driver at all. `lab_guest_configure` does that,
one tri-state toggle at a time, recording each value it replaced in
`C:\DriverLab\State\guest-config.json`:

```powershell
hyperv-lab --vm NORTH-Test init
hyperv-lab --vm NORTH-Test diagnostics            # Secure Boot, VBS/HVCI, test signing, dump mode
```

- If Secure Boot is On, the tool refuses — test signing and kernel debugging would be ignored.
  Pass `secure_boot_off=true` with the VM **Off**; that is its only action, and it then tells you
  to start the VM and call again.
- `testsigning=true` turns on the boot setting. It needs a reboot; `restart=true` performs one
  when `HYPERV_MCP_ALLOW_POWER` is on.
- `crash_dump=kernel` (or `complete`) is what makes a bugcheck leave something to read.
  `guest_config_status` reports whether the pagefile is big enough for a complete dump
  (RAM plus about 257 MB); it reports, it does not resize.

Nothing here changes the host's Secure Boot, HVCI or test-signing, and none of it changes power
state on its own.

## 2. Build the package

A package is a directory with a schema-1 manifest: each relative POSIX path, its byte length and
its lowercase SHA-256, plus the entry point `Run-Lab.ps1` and, for symbol work, the driver's
`.pdb` beside its `.sys`.

```text
example/
  Run-Lab.ps1
  MyDriver.sys
  MyDriver.pdb
```

Limits and rejections are in [LAB.md](LAB.md#manifest-and-transfer-integrity): 512 files, 2 GiB,
ASCII portable relative paths, no traversal, no reserved device names, no symlinks. Staging is
all-or-nothing — the host checks every file, the guest reports back what it wrote, and the host
decides whether that is the package that was approved.

```powershell
hyperv-lab --vm NORTH-Test stage C:\...\example --package-id example.1 --receipt .\stage-receipt.json
hyperv-lab --vm NORTH-Test verify example.1 <manifest-sha256-from-receipt>
```

Keep the `manifest_sha256`. Every run is asked to match it, and a mismatch is `not_verified`
with nothing executed.

## 3. Turn on Driver Verifier

`lab_verifier` writes `VerifyDriverLevel` and `VerifyDrivers` in the guest registry — not
`verifier /query`, whose output is translated on a localized guest.

```json
{"action": "enable", "drivers": ["MyDriver.sys"], "flags": ["standard"]}
```

- `drivers` omitted applies the flags to every driver; naming module names such as
  `mydriver.sys` (never paths) keeps the noise down.
- `flags` is `standard`, `all`, or a numeric mask; `standard` is the default.
- The previous setting is recorded in `C:\DriverLab\State\verifier.json`; `action="revert"`
  restores it and removes the file, so an operator's own Verifier configuration survives the lab.
- A change takes effect on the **next restart**, and the tool never restarts the VM.

`lab_diagnostics` reports the current state, which is also how a run's report can be checked
against what was actually on.

## 4. Run the cycle

In the lab profile, `lab_cycle` does the deliberate sequence without a human click:

1. read `vm_get` and refuse unless the VM is already Running;
2. create checkpoint `pre-<run_id>`;
3. re-verify the published package from the guest's own listing of it;
4. run `Invoke-DriverLabRun.ps1` with the approval phrase the server generates;
5. read `C:\DriverLab\Runs\<run_id>\result.json` and classify the outcome;
6. collect evidence (kernel dump included by default) and, with `revert_after=true`, restore
   `pre-<run_id>`.

With `debugger="kdnet"` or `"serial"` the cycle also attaches kd before step 4 and arms
`sxe ld:<driver>` for every `.sys` in the manifest, so the debugger stops at load instead of
after the crash.

`outcome` is `ok`, `failed`, `not_verified`, `bugcheck`, `hang` or `timeout`. Use a new `run_id`
for every attempt: a repeated id is a different run, not a retry of the same one.

Outside the lab profile the cycle is refused, and running a package is a deliberate guest action by an elevated operator — the runner demands an exact approval phrase, which is why the profile, not the caller, is what makes the loop autonomous. See [LAB.md](LAB.md#manual-run-boundary).

The runner checks administrator and hypervisor presence, the phrase, the manifest hash and every
file hash, and refuses when the package does not carry `Run-Lab.ps1`.

With `record=true` the cycle also keeps a GIF of the guest's console, sampled every two seconds and
written into the run's directory beside the evidence. It is the picture of where the run was when it
stopped, not a video stream, and it never fails the cycle: an unreadable frame is one frame fewer.

## 4a. Run it on several VMs

`lab_matrix(vm_names, package_id, run_id, expected_manifest_sha256, ...)` takes two to four VMs and
runs the same cycle on each, one after another, giving every VM its own run id (`<run_id>-<vm>`) and
its own checkpoint. It returns the rows plus a field-by-field comparison, so "passes on Windows 10,
bugchecks on Windows 11, blamed driver `MyDriver.sys`" is one table rather than two receipts. One
approval covers the whole matrix and names every VM; a VM that refuses the run is a row with its
error code, not the end of the matrix. The runs are sequential on purpose: each cycle owns the guest
worker, a checkpoint, the debugger and the console for its whole length.

## 5. Read the crash

A `bugcheck` is triaged rather than left at the code. `lab/crash.py` picks the dump the cycle
collected — newest file under `minidumps/`, falling back to `MEMORY.DMP` — runs `!analyze -v`
against it with kd on the host, and merges that reading with the host's own event 18590 record
(`Microsoft-Windows-Hyper-V-Worker-Admin`), which carries the bugcheck code and parameters.

The merge is what keeps a run honest: if kd cannot open the dump or is not installed, the host's
numbers stay and `crash.error` (with `crash.message`) says why; `crash.reason` says when there
was no dump to read at all. Read `report.crash.bugcheck` for:

| Field | Meaning |
|---|---|
| `bugcheck_code`, `bugcheck_parameters` | from kd, or from the host's event |
| `bugcheck_str` | the symbolic name, such as `DRIVER_IRQL_NOT_LESS_OR_EQUAL` |
| `failure_bucket_id` | the bucket `!analyze -v` computed |
| `blamed_driver` | the module kd points at |
| `symbols_resolved` | whether the names in the stack are real |
| `top_frames` | the stack as kd printed it |
| `host_event`, `sources` | the host's own record, and which halves contributed |

The full kd output is kept beside the evidence as `kd-analyze.log`. `vm_inject_nmi` forces a
hung guest into the debugger when a hang has to become a crash to produce a dump.

## 6. Symbols

A stack with `MyDriver!Crash+0x1a` is a symbol; `MyDriver+0x1a` is the module plus an offset,
which is what kd prints when the PDB was not found. The lab answers that question directly:

- `lab_cycle`'s report carries `debugger.symbols`, naming each driver the package ships as
  resolved or not (`kd_symbols` under the hood: `!lmi <module>`, falling back to `lm m <module>`).
- `crash.bugcheck.symbols_resolved` derives the same verdict from the dump reading, so a run
  with no debugger attached still says whether its names are real.

Symbols come from the Microsoft symbol server into the local cache, plus any `.pdb` the staged
package ships. A PDB that kd found and rejected (`*** Mismatched PDB ***`) counts as unresolved:
the stack has no names either way.

## 7. The record

Every cycle writes `lab-cycle.json` beside the evidence (`%LOCALAPPDATA%\HyperVConsoleMCP\evidence\<run_id>\`).
It holds the package id and the manifest hash the run was asked to match, the checkpoint and
whether it was reverted, the outcome and its detail, the crash reading, the symbol result, the
evidence files with one hash over the whole set, and the duration. Two runs of the same package
are then a file diff:

```powershell
git diff --no-index run-1\lab-cycle.json run-2\lab-cycle.json
```

The record is written last and never fails the cycle: it is a copy of the answer, not the answer.

## 8. Iterating

- Revert the checkpoint between attempts (`revert_after=true`) rather than cleaning up by hand;
  a bugcheck may have left the guest mid-state.
- Preserve evidence before retrying. `lab_collect` is not a rollback and does not remove guest
  artifacts.
- Driver Verifier stays on until reverted. A leak found under Verifier that never appears
  without it is still a finding: say so in the record rather than turning Verifier off and
  calling the run clean.
- A driver service installed with `lab_driver_service` is recorded too; `action="status"` shows
  what this tool installed, and the removal path (`action="remove"`) is explicit rather than
  implied by a reboot.

## What this flow does not do

- It does not sign anything, and it does not turn off signature enforcement. A driver the guest
  refuses is reported with its Win32 error and NTSTATUS and stays refused.
- It does not generate malformed input. A fuzzing harness for a driver's own IOCTL interface is
  the operator's `Run-Lab.ps1` — it belongs in the package, under the same approval, not behind
  an AI-callable tool.
- It does not prove a driver safe. A checkpoint, a debugger attachment and a hash match do not
  prevent a bugcheck; they make one readable.
