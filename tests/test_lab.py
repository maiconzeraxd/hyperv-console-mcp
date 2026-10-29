from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import PolicyError, ValidationError
from hyperv_console_mcp.lab.transfer import (
    DriverLabTransfer,
    build_manifest,
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
            return {"file": {"sha256": hashlib.sha256(data).hexdigest(), "bytes_written": len(data)}}
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
    assert backend.calls.count("guest_assemble_file") == 1
    assert backend.files[r"c:\driverlab\packages\north-test.1\driver.bin"] == large
    assert backend.files[r"c:\driverlab\packages\north-test.1\empty.log"] == b""
    assert DriverLabTransfer(service).verify("NORTH-Test", "north-test.1", result["manifest_sha256"])["verified"]


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


def test_mcp_lab_source_must_be_under_configured_root(guest, tmp_path: Path, monkeypatch) -> None:
    service, backend = guest
    source = tmp_path / "outside"
    source.mkdir()
    (source / "a.txt").write_text("data")
    monkeypatch.setenv("HYPERV_MCP_LAB_ROOT", str(tmp_path / "allowed"))
    with pytest.raises(PolicyError):
        service.lab_stage("NORTH-Test", str(source))
    assert backend.calls == []
