from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import BackendError, PolicyError, ValidationError
from hyperv_console_mcp.lab.cli import _tool_files, initialize
from hyperv_console_mcp.lab.transfer import (
    DriverLabTransfer,
    build_manifest,
    canonical_json,
)
from hyperv_console_mcp.service import HyperVService


class FakeGuestBackend:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.corrupt_download = False

    @staticmethod
    def _path(payload: dict[str, Any]) -> str:
        return str(payload["path"]).casefold()

    def invoke(self, operation: str, payload: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        self.calls.append(operation)
        if operation == "guest_write_file":
            path = self._path(payload)
            assert payload["overwrite"] or path not in self.files
            data = base64.b64decode(payload["content_base64"])
            self.files[path] = data
            return {"file": {"sha256": hashlib.sha256(data).hexdigest(), "bytes_written": len(data)}}
        if operation == "guest_assemble_file":
            directory = payload["parts_directory"].casefold()
            parts = [self.files[f"{directory}\\part-{index:06d}"] for index in range(payload["part_count"])]
            data = b"".join(parts)
            assert len(data) == payload["expected_size"]
            assert hashlib.sha256(data).hexdigest() == payload["expected_sha256"]
            self.files[self._path(payload)] = data
            for key in [key for key in self.files if key.startswith(directory + "\\")]:
                del self.files[key]
            return {"file": {"sha256": hashlib.sha256(data).hexdigest(), "bytes_written": len(data)}}
        if operation == "guest_publish_package":
            staging = rf"c:\driverlab\transfers\{payload['transfer_id']}\{payload['package_id']}".casefold()
            final = rf"c:\driverlab\packages\{payload['package_id']}".casefold()
            staged = {
                key: data for key, data in self.files.items() if key.startswith(staging + "\\")
            }
            assert staged, "nothing was staged"
            manifest = json.loads(staged.pop(staging + r"\lab-manifest.json"))
            assert hashlib.sha256(canonical_json(manifest)).hexdigest() == payload["manifest_sha256"]
            for entry in manifest["files"]:
                key = staging + "\\" + entry["path"].replace("/", "\\").casefold()
                data = staged.pop(key)
                assert hashlib.sha256(data).hexdigest() == entry["sha256"]
                self.files[final + key[len(staging):]] = data
            assert not staged, f"unlisted staged files: {sorted(staged)}"
            for key in [key for key in self.files if key.startswith(staging + "\\")]:
                del self.files[key]
            return {"package": {
                "package_id": payload["package_id"],
                "guest_path": rf"C:\DriverLab\Packages\{payload['package_id']}",
                "files_verified": len(manifest["files"]),
                "manifest_sha256": payload["manifest_sha256"],
            }}
        if operation == "guest_read_chunk":
            data = self.files[self._path(payload)][payload["offset"]:payload["offset"] + payload["count"]]
            assert len(data) == payload["count"]
            if self.corrupt_download:
                data = bytes([data[0] ^ 1]) + data[1:]
            return {"chunk": {"content_base64": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest()}}
        if operation == "guest_read_file":
            data = self.files[self._path(payload)]
            assert len(data) <= payload["max_bytes"]
            return {"file": {"content_base64": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest()}}
        if operation == "guest_exec":
            return {"execution": {"success": True, "exit_code": 0, "output": "verified"}}
        raise AssertionError(operation)


@pytest.fixture
def guest() -> tuple[HyperVService, FakeGuestBackend]:
    backend = FakeGuestBackend()
    service = HyperVService(backend, Settings(
        allowed_vms=frozenset({"NORTH-Test"}), allow_guest=True,
        guest_username="Administrator", guest_password="secret",
    ))
    return service, backend


def test_large_package_chunks_empty_files_and_hashes(guest, tmp_path: Path) -> None:
    service, backend = guest
    source = tmp_path / "package"
    source.mkdir()
    large = bytes(range(256)) * 10_000  # 2.44 MiB: three 1 MiB chunks.
    (source / "driver.bin").write_bytes(large)
    (source / "empty.log").write_bytes(b"")
    manifest = build_manifest(source, "north-test.1")
    result = DriverLabTransfer(service).stage("NORTH-Test", source, manifest, chunk_bytes=1024 * 1024)
    assert result["payload_executed"] is False
    assert result["files"][0]["chunks"] == 3
    assert result["files_verified"] == 2
    assert backend.calls.count("guest_assemble_file") == 1
    assert backend.calls.count("guest_publish_package") == 1
    assert backend.files[r"c:\driverlab\packages\north-test.1\driver.bin"] == large
    assert backend.files[r"c:\driverlab\packages\north-test.1\empty.log"] == b""
    assert not [path for path in backend.files if path.startswith(r"c:\driverlab\transfers")]
    assert DriverLabTransfer(service).verify("NORTH-Test", "north-test.1", result["manifest_sha256"])["verified"]


def test_staging_never_publishes_a_package_that_fails_verification(guest, tmp_path: Path) -> None:
    service, backend = guest
    source = tmp_path / "package"
    source.mkdir()
    (source / "driver.sys").write_bytes(b"binary")
    manifest = build_manifest(source, "atomic.1")
    # The guest reports a different hash than the host computed, as a torn write would.
    original = backend.invoke

    def tampering(operation: str, payload: dict[str, Any], *, timeout: float | None = None):
        result = original(operation, payload, timeout=timeout)
        if operation == "guest_write_file" and payload["path"].endswith("driver.sys"):
            result["file"]["sha256"] = "0" * 64
        return result

    backend.invoke = tampering  # type: ignore[method-assign]
    with pytest.raises(ValidationError, match="hash mismatch"):
        DriverLabTransfer(service).stage("NORTH-Test", source, manifest)
    assert "guest_publish_package" not in backend.calls
    assert not [path for path in backend.files if path.startswith(r"c:\driverlab\packages")]


def test_package_paths_and_mutation_are_rejected(guest, tmp_path: Path) -> None:
    service, backend = guest
    source = tmp_path / "package"
    source.mkdir()
    path = source / "test.bin"
    path.write_bytes(b"original")
    manifest = build_manifest(source, "safe")
    path.write_bytes(b"changed")
    with pytest.raises(ValidationError, match="changed"):
        DriverLabTransfer(service).stage("NORTH-Test", source, manifest)
    assert backend.calls == []
    manifest["files"][0]["path"] = "../escape.sys"
    with pytest.raises(ValidationError):
        DriverLabTransfer(service).stage("NORTH-Test", source, manifest)
    manifest["files"][0]["size"] = "not-an-integer"
    with pytest.raises(ValidationError):
        DriverLabTransfer(service).stage("NORTH-Test", source, manifest)
    assert backend.calls == []


def test_evidence_download_is_chunked_and_verified(guest, tmp_path: Path) -> None:
    service, backend = guest
    data = b"evidence" * 160_000
    digest = hashlib.sha256(data).hexdigest()
    run = r"c:\driverlab\evidence\run-1"
    backend.files[run + r"\logs\run.log"] = data
    index = {"schema": 1, "files": [{"path": "logs/run.log", "size": len(data), "sha256": digest}]}
    backend.files[run + r"\evidence.json"] = json.dumps(index).encode()
    result = DriverLabTransfer(service).collect("NORTH-Test", "north-test.1", "run-1", tmp_path, chunk_bytes=1024 * 1024)
    assert result["files"][0]["sha256"] == digest
    assert (tmp_path / "run-1" / "logs" / "run.log").read_bytes() == data
    assert (tmp_path / "run-1" / "host-receipt.json").is_file()
    assert not list((tmp_path / "run-1").rglob("*.tmp"))


def test_evidence_chunk_corruption_leaves_no_final_file(guest, tmp_path: Path) -> None:
    service, backend = guest
    data = b"x" * (1024 * 1024 + 1)
    run = r"c:\driverlab\evidence\run-bad"
    backend.files[run + r"\bad.log"] = data
    backend.files[run + r"\evidence.json"] = json.dumps({"schema": 1, "files": [{"path": "bad.log", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]}).encode()
    backend.corrupt_download = True
    with pytest.raises(ValidationError, match="chunk|SHA-256"):
        DriverLabTransfer(service).collect("NORTH-Test", "safe", "run-bad", tmp_path, chunk_bytes=1024 * 1024)
    assert not (tmp_path / "run-bad" / "bad.log").exists()
    assert not list((tmp_path / "run-bad").glob("*.tmp"))


def test_mcp_lab_source_must_be_under_configured_root(tmp_path: Path) -> None:
    backend = FakeGuestBackend()
    service = HyperVService(backend, Settings(
        allowed_vms=frozenset({"NORTH-Test"}), allow_guest=True,
        guest_username="Administrator", guest_password="secret",
        lab_root=tmp_path / "allowed",
    ))
    source = tmp_path / "outside"
    source.mkdir()
    (source / "a.txt").write_text("data")
    with pytest.raises(PolicyError):
        service.lab_stage("NORTH-Test", str(source))
    assert backend.calls == []


def test_mcp_lab_source_is_accepted_under_the_configured_root(tmp_path: Path) -> None:
    backend = FakeGuestBackend()
    service = HyperVService(backend, Settings(
        allowed_vms=frozenset({"NORTH-Test"}), allow_guest=True,
        guest_username="Administrator", guest_password="secret",
        lab_root=tmp_path / "allowed",
    ))
    source = tmp_path / "allowed" / "package"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("data")
    result = service.lab_stage("NORTH-Test", str(source), "root-test")
    assert result["guest_path"].endswith(r"\Packages\root-test")


class FakeInitService:
    """Minimal service stub for the lab tool installation path."""

    def __init__(self, error_code: str | None) -> None:
        self.error_code = error_code
        self.written: list[str] = []
        self.executed = False

    def guest_read_file(self, vm_name: str, path: str, *, max_bytes: int) -> dict[str, Any]:
        if self.error_code is not None:
            raise BackendError("guest read failed", code=self.error_code)
        data = Path(path).name.encode()
        return {"file": {"sha256": hashlib.sha256(data).hexdigest()}}

    def guest_write_file(self, vm_name: str, path: str, content: str, **_: Any) -> dict[str, Any]:
        self.written.append(path)
        return {"file": {"sha256": hashlib.sha256(base64.b64decode(content)).hexdigest()}}

    def guest_exec(self, vm_name: str, command: str) -> dict[str, Any]:
        self.executed = True
        return {"execution": {"success": True, "exit_code": 0, "output": "{}"}}


def test_lab_init_installs_tools_when_the_guest_reports_them_missing() -> None:
    service = FakeInitService("not_found")
    result = initialize(service, "NORTH-Test")
    assert result["already_verified"] == 0
    assert len(service.written) == len(_tool_files())
    assert service.executed is True


def test_lab_init_does_not_overwrite_tools_after_a_generic_bridge_error() -> None:
    service = FakeInitService("bridge_error")
    with pytest.raises(BackendError):
        initialize(service, "NORTH-Test")
    assert service.written == []
    assert service.executed is False
