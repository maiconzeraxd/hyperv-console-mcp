from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from hyperv_console_mcp.backend import PowerShellBackend
from hyperv_console_mcp.errors import BackendError


def test_bridge_response_is_base64_json() -> None:
    value = {"ok": True, "result": {"vms": [], "message": "Olá"}}
    line = "HVMCP1:" + base64.b64encode(json.dumps(value).encode()).decode()
    assert PowerShellBackend._decode("noise\n" + line) == value


def test_bridge_rejects_malformed_response() -> None:
    with pytest.raises(BackendError):
        PowerShellBackend._decode("HVMCP1:not-base64")
    assert PowerShellBackend._decode("unstructured") is None


def test_bridge_missing_script() -> None:
    with pytest.raises(BackendError):
        PowerShellBackend(Path("does-not-exist.ps1"))
