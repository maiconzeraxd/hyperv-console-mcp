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
