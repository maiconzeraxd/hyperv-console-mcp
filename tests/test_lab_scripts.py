from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hyperv_console_mcp.errors import ValidationError
from hyperv_console_mcp.lab.transfer import (
    DriverLabTransfer,
    build_manifest,
    manifest_hash,
)


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell.exe") is None,
    reason="Windows PowerShell is required.",
)
def test_powershell_canonical_manifest_hash_matches_python(tmp_path: Path) -> None:
    source = tmp_path / "package"
    source.mkdir()
    (source / "driver.sys").write_bytes(b"not executed")
    (source / "notes.txt").write_text('ASCII "quotes" and \\slash', encoding="ascii")
    manifest = build_manifest(source, "hash-test.1")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    module = Path(__file__).parents[1] / "src" / "hyperv_console_mcp" / "lab" / "guest" / "DriverLab.Common.psm1"
    command = (
        f"Import-Module -Name '{str(module).replace(chr(39), chr(39) * 2)}' -Force; "
        f"$m = Get-Content -LiteralPath '{str(path).replace(chr(39), chr(39) * 2)}' -Raw | ConvertFrom-Json; "
        "Get-LabManifestHash -Manifest $m"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == manifest_hash(manifest)


@pytest.mark.parametrize("name", ["../escape.sys", "café.sys", "bad?.sys", "NUL", "folder."])
def test_manifest_rejects_nonportable_windows_paths(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValidationError):
        DriverLabTransfer._entry_path({"path": name})


def test_case_insensitive_manifest_duplicates_are_rejected(tmp_path: Path) -> None:
    entry = {"size": 1, "sha256": "0" * 64}
    manifest = {"schema": 1, "package_id": "test", "files": [
        {"path": "driver.sys", **entry}, {"path": "DRIVER.SYS", **entry},
    ]}
    with pytest.raises(ValidationError, match="duplicate"):
        DriverLabTransfer._validate_manifest(manifest)
