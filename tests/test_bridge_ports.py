from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hyperv_console_mcp.backend import PowerShellBackend

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell is required.")


def test_com_port_status_uses_hyperv_port_names(tmp_path: Path) -> None:
    bridge = Path(__file__).parents[1] / "src" / "hyperv_console_mcp" / "bridge" / "HyperVBridge.ps1"
    escaped = str(bridge).replace("'", "''")
    wrapper = tmp_path / "MockHyperV.ps1"
    wrapper.write_text(
        f"""
param([switch]$Server)
function Import-Module {{ param($Name, $ErrorAction) }}
function Get-VM {{ param($Name) [pscustomobject]@{{ Name = 'NORTH-Test'; State = 'Running' }} }}
function Get-VMComPort {{
    param($Vm)
    [pscustomobject]@{{ Name = 'COM 1'; Path = '\\\\.\\pipe\\north-kd'; DebuggerMode = 'On' }}
    [pscustomobject]@{{ Name = 'COM 2'; Path = $null; DebuggerMode = 'Off' }}
}}
& '{escaped}' -Server
""",
        encoding="utf-8",
    )
    backend = PowerShellBackend(wrapper)
    try:
        result = backend.invoke("debug_serial_status", {"vm": "NORTH-Test"})
    finally:
        backend.close()
    assert result["com_ports"] == [
        {"number": 1, "path": r"\\.\pipe\north-kd", "debugger_mode": "On"},
        {"number": 2, "path": None, "debugger_mode": "Off"},
    ]
