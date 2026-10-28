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

PowerShell Direct still constructs a remoting session per guest command. It is the safe initial transport for isolated guests, but it is not claimed to be faster than a persistent SSH session. Do not enable SSH solely for OOBE or kernel debugging: neither use case can depend on a running guest userland.

## Benchmarks

The test suite includes a real persistent-worker test using a local fake PowerShell bridge, so the measurement does not touch Hyper-V. Run it with `pytest -s tests/test_persistent_backend.py`. The cold request includes PowerShell startup; the following nine requests reuse that process. On the development host, one measured run took 0.153 seconds cold and 0.002 seconds for nine warm calls. These are fake-worker figures, not a claim about Hyper-V or guest latency. A future SSH adapter must be compared on the same hardware and workload before being made the default.

## Recommended measurement matrix

1. Persistent local worker: VM status and checkpoint listing.
2. PowerShell Direct: ten small guest commands and a 1 MiB read.
3. Optional SSH: the same ten commands and file read over a private virtual switch.
4. KDNET versus serial: debugger attach time, break/resume latency, and reliability at early boot and bugcheck.

Do not use an external switch for a disposable driver-test VM just to obtain a marginal transport improvement. Isolation takes precedence over latency.

## Planned next optimization

A persistent PowerShell Direct runspace can reduce guest-command overhead, but adds session lifetime, cancellation, and secret-lifetime complexity. It should only replace the simple implementation after a measured benchmark and explicit error-handling tests.
