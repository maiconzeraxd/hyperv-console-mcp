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

## The window's console cadence

The console page is not a video stream: every frame is a full `console_screenshot` over the same pipe the AI uses, so the cadence is a budget, not a preference. `SCREEN_INTERVAL_MS` is 800 ms — roughly 1.25 frames a second, enough to follow a boot or a bugcheck countdown — and each capture is capped at 1600 px wide.

The loop is deliberately lazy in three ways:

- **One frame in flight.** The next timer is armed only after the previous promise settles, so a slow capture delays the next frame instead of queueing a backlog the page then has to drop.
- **A hidden or paused window issues no capture at all.** The timer keeps ticking so the page resumes instantly, but nothing crosses the pipe while the operator is on another tab or has pressed Pause, and the VM is unaffected either way.
- **A refusal stops the loop.** An Off VM, a paused VM or a level without screenshots is not a transient failure; the page says what the service said and stops asking, rather than retrying twice a second and filling the feed with the same error.

The thumbnail path is the only consumer of this cadence; `console_screenshot(source="vmconnect")` is requested once, by the button that opens it, and never on a timer.

No live frame-time numbers are recorded here yet. The figure that would matter is the round trip of one capture against a running guest on this host — a measurement `scripts/bench_transport.py` does not take, because it measures guest commands and file transfers rather than the VMConnect capture path.

## Recommended measurement matrix

1. Persistent local worker: VM status and checkpoint listing.
2. PowerShell Direct: ten small guest commands and a 1 MiB read.
3. Optional SSH: the same ten commands and file read over a private virtual switch.
4. KDNET versus serial: debugger attach time, break/resume latency, and reliability at early boot and bugcheck.

Do not use an external switch for a disposable driver-test VM just to obtain a marginal transport improvement. Isolation takes precedence over latency.

## Next step

The transport now reuses one session per VM, which was the first half of the "planned optimization" that used to be listed here. The remaining questions are the ones only a live run can answer: how much the session actually saves per command, whether `Copy-Item` over VMBus saturates at 1 MiB or 100 MiB, and whether a second worker is needed so a long `guest_exec` does not delay `vm_get` and the console tools.
