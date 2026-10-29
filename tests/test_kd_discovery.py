"""Finding kd.exe without ever falling back to the WinDbgX app."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyperv_console_mcp.errors import DebuggerError
from hyperv_console_mcp.kd import discovery


def test_an_explicit_directory_wins(tmp_path: Path) -> None:
    executable = tmp_path / "kd.exe"
    executable.write_bytes(b"MZ")

    assert discovery.find_debugger("kd", directory=tmp_path) == executable.resolve()


def test_an_unknown_debugger_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DebuggerError, match="Unknown debugger"):
        discovery.find_debugger("windbgx", directory=tmp_path)


def test_a_missing_debugger_explains_where_to_get_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery.shutil, "which", lambda name: None)
    monkeypatch.setattr(discovery, "DEFAULT_DIRECTORIES", ())

    with pytest.raises(DebuggerError) as error:
        discovery.find_debugger("kd", directory=tmp_path)

    assert error.value.code == "kd_not_found"
    assert "HYPERV_MCP_DEBUGGERS_DIR" in str(error.value)
    assert "Debugging Tools" in str(error.value)
