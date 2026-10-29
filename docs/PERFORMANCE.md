# Performance and transport choices

## Decision

| Workload | Preferred channel | Why |
|---|---|---|
| Inventory, VM lifecycle, checkpoints | Persistent local PowerShell process | Native Hyper-V integration; no guest agent or network dependency. |
| Console keyboard/mouse | Hyper-V WMI synthetic devices in the same worker | Works during OOBE, at the lock screen, and with no guest network. |
| Infrequent guest commands/files | PowerShell Direct | VMBus, no TCP/IP, works in an isolated VM. |
| High-frequency scripted guest administration | Optional SSH on an isolated virtual network | Persistent authenticated sessions reduce per-command setup overhead. |
| Kernel debugging with a suitable isolated network | KDNET | Preferred network debugger transport for iterative kernel work. |
| Kernel debugging in an isolated VM without a network | COM 1 over a Hyper-V named pipe | Preserves isolation and needs no guest NIC. |
| Guest visual capture | Experimental VMConnect window rendering | No documented Hyper-V guest-framebuffer API; never substitutes host desktop pixels. |

## What the server does now

The Python backend holds one local Windows PowerShell worker open. Requests are UTF-8 JSON lines and responses are base64-encoded JSON lines. A lock serializes each request/response pair so a failed or timed-out request cannot be confused with another tool call. The worker is discarded after a transport failure or timeout. Stdout is exclusively MCP protocol data; diagnostics go to stderr.

PowerShell Direct keeps one `New-PSSession -VMName` per VM inside the worker and reuses it for every guest request; a session that is no longer `Opened`, or whose VM uptime went down (a reboot), is dropped and rebuilt. File copies go over that session with `Copy-Item -ToSession`/`-FromSession` and are SHA-256 verified on both sides, so the file content never travels as base64 inside a JSON line. It is still the safe default for isolated guests, and it is still not claimed to be faster than a persistent SSH session: that claim needs the measurement below. Do not enable SSH solely for OOBE or kernel debugging: neither use case can depend on a running guest userland.

Serial-console reads do not occupy the PowerShell worker. Each attachment owns a cancellable Windows named-pipe handle and a dedicated reader thread. It retains at most 1,048,576 characters in memory and appends output to the caller-selected local `.log` file. This prevents a quiet or disconnected COM pipe from stalling unrelated VM operations.

## Benchmarks

The test suite includes a real persistent-worker test using a local fake PowerShell bridge, so the measurement does not touch Hyper-V. Run it with `pytest -s tests/test_persistent_backend.py`. The cold request includes PowerShell startup; the following nine requests reuse that process. On the development host, one measured run took 0.153 seconds cold and 0.002 seconds for nine warm calls. These are fake-worker figures, not a claim about Hyper-V or guest latency. A future SSH adapter must be compared on the same hardware and workload before being made the default.

## Measuring a real transport

`scripts/bench_transport.py` measures the configured transport against a disposable VM: ten small guest commands, then an upload and a download for each requested size (1 MiB and 100 MiB by default). It reports the median/min/max of the command sample and the seconds and MiB/s of each transfer, and the JSON result carries the host name, the OS build, the VM and the date, because a number without those is not a measurement.

```
set HYPERV_MCP_LIVE_TEST_VM=LAB-Test
python scripts/bench_transport.py --vm LAB-Test --json-out bench.json
```

It writes and removes `C:\DriverLab\Bench\` inside the guest, so only point it at a VM you can throw away. **No live numbers have been recorded yet**: the script has not been run against a VM in this delivery, and this document deliberately does not guess at what it would print.

## Recommended measurement matrix

1. Persistent local worker: VM status and checkpoint listing.
2. PowerShell Direct: ten small guest commands and a 1 MiB read.
3. Optional SSH: the same ten commands and file read over a private virtual switch.
4. KDNET versus serial: debugger attach time, break/resume latency, and reliability at early boot and bugcheck.

Do not use an external switch for a disposable driver-test VM just to obtain a marginal transport improvement. Isolation takes precedence over latency.

## Next step

The transport now reuses one session per VM, which was the first half of the "planned optimization" that used to be listed here. The remaining questions are the ones only a live run can answer: how much the session actually saves per command, whether `Copy-Item` over VMBus saturates at 1 MiB or 100 MiB, and whether a second worker is needed so a long `guest_exec` does not delay `vm_get` and the console tools.
