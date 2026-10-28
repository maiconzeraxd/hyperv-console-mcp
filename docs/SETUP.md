# MCP client configuration

Create a protected environment for the MCP host. Do not commit real passwords.

```powershell
$env:HYPERV_MCP_ALLOWED_VMS = 'NORTH-Test'
$env:HYPERV_MCP_ALLOW_POWER = 'false'
$env:HYPERV_MCP_ALLOW_CHECKPOINTS = 'false'
$env:HYPERV_MCP_ALLOW_GUEST = 'false'
$env:HYPERV_MCP_ALLOW_CONSOLE = 'false'
$env:HYPERV_MCP_ALLOW_SCREENSHOTS = 'false'
$env:HYPERV_MCP_ALLOW_DEBUGGER = 'false'
$env:HYPERV_MCP_ALLOW_MEDIA = 'false'
```

For PowerShell Direct only, additionally set `HYPERV_MCP_GUEST_USERNAME` and `HYPERV_MCP_GUEST_PASSWORD` in the protected host environment. They are not command-line arguments or tool parameters.

Example host configuration using Python directly:

```json
{
  "mcpServers": {
    "hyperv": {
      "command": "C:\\Path\\To\\Python\\python.exe",
      "args": [
        "-I",
        "C:\\Path\\To\\hyperv-console-mcp\\scripts\\run_server.py"
      ],
      "env": {
        "HYPERV_MCP_ALLOWED_VMS": "NORTH-Test",
        "HYPERV_MCP_ALLOW_POWER": "false",
        "HYPERV_MCP_ALLOW_CHECKPOINTS": "false",
        "HYPERV_MCP_ALLOW_GUEST": "false",
        "HYPERV_MCP_ALLOW_CONSOLE": "false",
        "HYPERV_MCP_ALLOW_SCREENSHOTS": "false",
        "HYPERV_MCP_ALLOW_DEBUGGER": "false",
        "HYPERV_MCP_ALLOW_MEDIA": "false"
      }
    }
  }
}
```

After an editable/package installation, `hyperv-console-mcp` can be the command instead. The server runs only on stdio; it does not listen on TCP.

The process must have Hyper-V management permission. Run the MCP host elevated or grant the intended account Hyper-V Administrators access through your normal administrative process. The server never changes host group membership or UAC settings.

## Confined host paths

ISO mounts accept only files under `HYPERV_MCP_ISO_ROOT` (default `%LOCALAPPDATA%\HyperVConsoleMCP\media`). Serial logs must remain under `HYPERV_MCP_SERIAL_LOG_ROOT` (default `%LOCALAPPDATA%\HyperVConsoleMCP\serial`). Set both roots explicitly when using a different library or protected log directory:

```powershell
$env:HYPERV_MCP_ISO_ROOT = 'C:\VMs_iso'
$env:HYPERV_MCP_SERIAL_LOG_ROOT = "$env:LOCALAPPDATA\HyperVConsoleMCP\serial"
```

## Dashboard

Start the native interface from the repository with:

```powershell
python -I scripts\run_dashboard.py --vm NORTH-Test
```

For optional local approval of every AI-requested mutation, set `HYPERV_MCP_REQUIRE_DASHBOARD_APPROVAL=true` in both MCP and dashboard environments and leave the dashboard open. See [DASHBOARD.md](DASHBOARD.md).

## Reproducible lab

`HYPERV_MCP_LAB_ROOT` restricts host package sources; `HYPERV_MCP_EVIDENCE_ROOT` controls evidence downloads. `HYPERV_MCP_EVENT_DIRECTORY` selects the local event store shared by the dashboard and MCP. Do not place credentials in these paths or in `.env.example`.

See [LAB.md](LAB.md) for the non-executing staging/verification workflow and the separate manual guest runner. No staging or diagnostic step authorizes loading a driver.
