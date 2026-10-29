# Kernel debugging with WinDbg

This page moved to **[WINDBG.md](WINDBG.md)**, which covers the whole path: choosing between
KDNET and the COM1 named pipe, `debug_kdnet_prepare`/`debug_serial_prepare`, the `kd.exe`
session tools, symbol configuration, the bugcheck flow, and opening a saved dump.

The short version: KDNET for frequent kernel work on the internal `HyperV-MCP-Lab` switch,
COM1/named-pipe for a VM that must stay off the network. SSH is a guest-administration
transport and cannot replace either one for boot debugging, breakpoints or bugchecks.
