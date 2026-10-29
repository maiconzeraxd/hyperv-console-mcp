from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.errors import ValidationError
from hyperv_console_mcp.lab.publish import entry_path, validate_manifest
from hyperv_console_mcp.lab.transfer import (
    build_manifest,
    canonical_json,
    manifest_hash,
)

MODULE = (
    Path(__file__).parents[1] / "src" / "hyperv_console_mcp" / "lab" / "guest" / "DriverLab.Common.psm1"
)
requires_powershell = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell.exe") is None,
    reason="Windows PowerShell is required.",
)


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def powershell_manifest_hash(tmp_path: Path, manifest: dict[str, Any]) -> str:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    command = (
        f"Import-Module -Name {_quote(str(MODULE))} -Force; "
        f"$m = Get-Content -LiteralPath {_quote(str(path))} -Raw | ConvertFrom-Json; "
        "Get-LabManifestHash -Manifest $m"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


@requires_powershell
def test_powershell_canonical_manifest_hash_matches_python(tmp_path: Path) -> None:
    source = tmp_path / "package"
    source.mkdir()
    (source / "driver.sys").write_bytes(b"not executed")
    (source / "notes.txt").write_text('ASCII "quotes" and \\slash', encoding="ascii")
    manifest = build_manifest(source, "hash-test.1")
    assert powershell_manifest_hash(tmp_path, manifest) == manifest_hash(manifest)


@requires_powershell
def test_powershell_canonical_hash_matches_python_for_special_characters(
    tmp_path: Path,
) -> None:
    # PowerShell 5.1's ConvertTo-Json escapes & ' < > while Python leaves them literal,
    # so the module must serialize strings by hand to keep the two hashes equal.
    manifest = {
        "schema": 1,
        "package_id": "chars-1",
        "note": "amp & lt < gt > quote ' dquote \" slash \\ control \u0001 tab \t",
        "files": [{"path": "driver.sys", "size": 1, "sha256": "a" * 64}],
    }
    assert powershell_manifest_hash(tmp_path, manifest) == manifest_hash(manifest)


@requires_powershell
def test_powershell_canonical_hash_orders_keys_ordinally(tmp_path: Path) -> None:
    # "Z" sorts before "a" ordinally but after it under a culture-aware comparison.
    manifest = {"schema": 1, "Zeta": 1, "alpha": 2, "files": []}
    assert powershell_manifest_hash(tmp_path, manifest) == manifest_hash(manifest)


@requires_powershell
def test_powershell_path_validation_matches_python(tmp_path: Path) -> None:
    rejected = ["../escape.sys", "café.sys", "bad?.sys", "NUL", "folder.", "my file.sys"]
    accepted = ["driver.sys", "sub/dir-1/a_b.c", ".hidden"]
    path = tmp_path / "paths.json"
    path.write_text(json.dumps({"rejected": rejected, "accepted": accepted}), encoding="utf-8")
    command = (
        f"Import-Module -Name {_quote(str(MODULE))} -Force; "
        f"$spec = Get-Content -LiteralPath {_quote(str(path))} -Raw | ConvertFrom-Json; "
        "$rejectedPassed = 0; "
        "foreach ($value in @($spec.rejected)) { "
        "  try { $null = Get-LabRelativePath -Path $value; $rejectedPassed++ } catch { } }; "
        "$acceptedFailed = 0; "
        "foreach ($value in @($spec.accepted)) { "
        "  try { $null = Get-LabRelativePath -Path $value } catch { $acceptedFailed++ } }; "
        "Write-Output ($rejectedPassed.ToString() + '|' + $acceptedFailed.ToString())"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "0|0"
    for value in rejected:
        with pytest.raises(ValidationError):
            entry_path({"path": value})
    for value in accepted:
        assert entry_path({"path": value}) == value


@pytest.mark.parametrize(
    "name",
    ["../escape.sys", "café.sys", "bad?.sys", "NUL", "folder.", "my file.sys", "a//b.sys", "./a.sys"],
)
def test_manifest_rejects_nonportable_windows_paths(name: str) -> None:
    with pytest.raises(ValidationError):
        entry_path({"path": name})


def test_case_insensitive_manifest_duplicates_are_rejected(tmp_path: Path) -> None:
    entry = {"size": 1, "sha256": "0" * 64}
    manifest = {"schema": 1, "package_id": "test", "files": [
        {"path": "driver.sys", **entry}, {"path": "DRIVER.SYS", **entry},
    ]}
    with pytest.raises(ValidationError, match="duplicate"):
        validate_manifest(manifest)


PACKAGE_SCRIPT = MODULE.parent / "Test-DriverLabPackage.ps1"


def write_package(root: Path, package_id: str, files: dict[str, bytes]) -> str:
    package = root / "Packages" / package_id
    package.mkdir(parents=True)
    entries = []
    for name, data in files.items():
        target = package.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        entries.append({
            "path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        })
    manifest = {"schema": 1, "package_id": package_id, "files": entries}
    (package / "lab-manifest.json").write_bytes(canonical_json(manifest))
    return manifest_hash(manifest)


@requires_powershell
def test_package_verification_accepts_a_clean_package(tmp_path: Path) -> None:
    digest = write_package(tmp_path, "clean.1", {"driver.sys": b"binary", "sub/notes.txt": b"x"})
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(PACKAGE_SCRIPT), "-PackageId", "clean.1",
         "-ExpectedManifestSha256", digest, "-Root", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["verified"] is True
    assert result["file_count"] == 2


@requires_powershell
def test_package_verification_rejects_files_missing_from_the_manifest(tmp_path: Path) -> None:
    digest = write_package(tmp_path, "sneaky.1", {"driver.sys": b"binary"})
    (tmp_path / "Packages" / "sneaky.1" / "extra.dll").write_bytes(b"unverified")
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(PACKAGE_SCRIPT), "-PackageId", "sneaky.1",
         "-ExpectedManifestSha256", digest, "-Root", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )
    assert completed.returncode != 0
    assert "not in the manifest" in completed.stderr
    assert "extra.dll" in completed.stderr


@requires_powershell
def test_run_entry_point_must_be_listed_in_the_manifest(tmp_path: Path) -> None:
    write_package(tmp_path, "unlisted.1", {"driver.sys": b"binary", "Run-Lab.ps1": b"# noop"})
    # Remove Run-Lab.ps1 from the manifest while leaving the file on disk.
    package = tmp_path / "Packages" / "unlisted.1"
    manifest = json.loads((package / "lab-manifest.json").read_text(encoding="utf-8"))
    manifest["files"] = [entry for entry in manifest["files"] if entry["path"] != "Run-Lab.ps1"]
    (package / "lab-manifest.json").write_bytes(canonical_json(manifest))
    command = (
        f"Import-Module -Name {_quote(str(MODULE))} -Force; "
        f"try {{ Get-LabEntryPointPath -PackageId 'unlisted.1' -Root {_quote(str(tmp_path))} }} "
        "catch { Write-Output $_.Exception.Message; exit 1 }"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )
    assert completed.returncode == 1
    assert "must list Run-Lab.ps1" in completed.stdout


@requires_powershell
def test_run_entry_point_is_returned_when_the_manifest_lists_it(tmp_path: Path) -> None:
    write_package(tmp_path, "listed.1", {"Run-Lab.ps1": b"# noop"})
    command = (
        f"Import-Module -Name {_quote(str(MODULE))} -Force; "
        f"Get-LabEntryPointPath -PackageId 'listed.1' -Root {_quote(str(tmp_path))}"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().endswith("Run-Lab.ps1")
