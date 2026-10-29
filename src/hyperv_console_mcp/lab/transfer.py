"""Reproducible, hash-verified driver-lab staging and evidence transfer."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ..errors import ValidationError
from ..events import EVENTS, ProgressReporter
from .publish import (
    MAX_MANIFEST_BYTES,
    MAX_PACKAGE_BYTES,
    MAX_PACKAGE_FILES,
    entry_path,
    identifier,
    validate_manifest,
    verify_staged,
)

CHUNK_BYTES = 4 * 1024 * 1024
MAX_ERROR_CHARS = 600
GUEST_ROOT = r"C:\DriverLab"


def guest_script(path: str, *arguments: str) -> str:
    """Invoke one of this server's own guest scripts, whatever the guest's policy.

    The guest's ExecutionPolicy belongs to the operator, and the server does not
    change it. These scripts ship with the server and are hash-verified before
    they run, so each call starts its own PowerShell that bypasses the policy for
    that one file instead of relaxing the session for everything else.
    """
    tail = "".join(f" {argument}" for argument in arguments)
    return (
        "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        f"-File '{path}'{tail}"
    )


def failure_detail(execution: dict[str, Any]) -> str:
    """The most specific reason a guest script failed, not the whole transcript.

    A guest script started with `-File` reports a terminating error through its
    exit code and its text output, so `error_records` is often empty and the
    output is the only place the reason exists.
    """
    records = execution.get("error_records")
    if isinstance(records, list) and records:
        return str(records[0]).strip()[:MAX_ERROR_CHARS]
    output = str(execution.get("output") or "").strip()
    if output:
        return output[:MAX_ERROR_CHARS]
    return f"exit code {execution.get('exit_code')}"


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _unverified(package_id: str, reason: str) -> dict[str, Any]:
    return {
        "package_id": package_id, "verified": False, "reason": reason,
        "payload_executed": False,
    }


def manifest_hash(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(manifest)).hexdigest()


def build_manifest(source: Path, package_id: str | None = None) -> dict[str, Any]:
    source = source.resolve()
    if not source.is_dir():
        raise ValidationError("source must be an existing directory.")
    identifier(package_id, "package_id") if package_id else None
    package_id = package_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    )
    files = []
    total = 0
    seen_paths = set()
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or not path.is_file():
            if path.is_symlink():
                raise ValidationError(f"Symbolic links are not accepted in a lab package: {path}")
            continue
        relative = PurePosixPath(path.relative_to(source).as_posix())
        entry_path({"path": relative.as_posix()})
        if relative.as_posix().casefold() in seen_paths:
            raise ValidationError(f"Package paths must be unique on Windows: {relative}")
        seen_paths.add(relative.as_posix().casefold())
        if relative.name == "lab-manifest.json" or relative.is_absolute() or ".." in relative.parts:
            raise ValidationError(f"Unsafe or reserved package path: {relative}")
        size = path.stat().st_size
        total += size
        if len(files) >= MAX_PACKAGE_FILES or total > MAX_PACKAGE_BYTES:
            raise ValidationError("The package exceeds the 512-file or 2 GiB limit.")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        files.append({"path": relative.as_posix(), "size": size, "sha256": digest.hexdigest()})
    if not files:
        raise ValidationError("source must contain at least one file.")
    return {
        "schema": 1,
        "package_id": package_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }


class DriverLabTransfer:
    """Uses the policy-gated service; never executes a staged payload."""

    def __init__(self, service: Any) -> None:
        self.service = service

    @staticmethod
    def guest_package_path(package_id: str) -> str:
        return rf"{GUEST_ROOT}\Packages\{identifier(package_id, 'package_id')}"

    def stage(
        self, vm_name: str, source: Path, manifest: dict[str, Any], *, chunk_bytes: int = CHUNK_BYTES
    ) -> dict[str, Any]:
        validate_manifest(manifest)
        if not 1024 * 1024 <= chunk_bytes <= 8 * 1024 * 1024:
            raise ValidationError("chunk_bytes must be between 1 MiB and 8 MiB.")
        source = source.resolve()
        package_id = manifest["package_id"]
        # Nothing is written under Packages until every byte has been staged and
        # re-verified: the whole transfer lands in Transfers\<id>\<package> first and a
        # single directory rename publishes it.
        transfer_id = uuid.uuid4().hex
        staging = rf"{GUEST_ROOT}\Transfers\{transfer_id}\{package_id}"
        transfers = []
        reporter = ProgressReporter(EVENTS, "package.stage", vm_name, uuid.uuid4().hex)
        total = sum(entry["size"] for entry in manifest["files"])
        completed = 0
        reporter.update(0, f"Preparing {len(manifest['files'])} verified package files.")
        for entry in manifest["files"]:
            relative = entry_path(entry)
            local = source.joinpath(*PurePosixPath(relative).parts)
            if not local.is_file() or local.is_symlink() or local.stat().st_size != entry["size"]:
                raise ValidationError(f"Package file is missing or changed size: {relative}")
            digest = hashlib.sha256()
            with local.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != entry["sha256"]:
                raise ValidationError(f"Package file changed after manifest creation: {relative}")
        for index, entry in enumerate(manifest["files"]):
            relative = entry_path(entry)
            local = source.joinpath(*PurePosixPath(relative).parts)
            remote = staging + "\\" + "\\".join(PurePosixPath(relative).parts)
            if entry["size"] <= chunk_bytes:
                result = self.service.guest_write_file(
                    vm_name, remote, base64.b64encode(local.read_bytes()).decode("ascii"),
                    create_parent=True,
                )
                chunks = 1
            else:
                chunks = self._stage_large(
                    vm_name, local, remote, entry, chunk_bytes, transfer_id, index
                )
                result = {"file": {"sha256": entry["sha256"], "bytes_written": entry["size"]}}
            actual = result["file"]["sha256"].casefold()
            if actual != entry["sha256"]:
                raise ValidationError(f"Guest hash mismatch after staging: {relative}")
            transfers.append({"path": relative, "size": entry["size"], "chunks": chunks})
            completed += entry["size"]
            reporter.update((completed * 100 // max(total, 1)) - 1, f"Verified {relative}.")
        metadata = self.service.guest_write_file(
            vm_name, staging + r"\lab-manifest.json",
            base64.b64encode(canonical_json(manifest)).decode("ascii"), create_parent=True,
        )
        digest = manifest_hash(manifest)
        published = self.service.guest_publish_package(
            vm_name, package_id, transfer_id, digest
        )["package"]
        return {
            "package_id": package_id, "guest_path": published["guest_path"],
            "manifest_sha256": digest, "files": transfers,
            "manifest_file": metadata["file"],
            "files_verified": published["files_verified"], "payload_executed": False,
        }

    def _stage_large(
        self, vm_name: str, local: Path, remote: str, entry: dict[str, Any],
        chunk_bytes: int, transfer_id: str, index: int,
    ) -> int:
        directory = rf"{GUEST_ROOT}\Transfers\{transfer_id}\parts\{index:04d}"
        count = (entry["size"] + chunk_bytes - 1) // chunk_bytes
        with local.open("rb") as stream:
            for index in range(count):
                data = stream.read(chunk_bytes)
                if not data:
                    raise ValidationError("Package file was truncated during staging.")
                self.service.guest_write_file(
                    vm_name, rf"{directory}\part-{index:06d}",
                    base64.b64encode(data).decode("ascii"), create_parent=True,
                )
            if stream.read(1):
                raise ValidationError("Package file grew during staging.")
        self.service.guest_assemble_file(
            vm_name, remote, directory, count, entry["size"], entry["sha256"]
        )
        return count

    def verify(self, vm_name: str, package_id: str, expected_hash: str) -> dict[str, Any]:
        """Re-verify a published package: the guest lists it, the host decides.

        The same split as publishing. ``guest_package_list`` returns the manifest
        bytes and the listing of what is on disk; ``verify_staged`` compares them
        with the digest the caller approved. A package that does not match is a
        result (``verified: false``) rather than an error, because ``lab_run``
        turns it into the ``not_verified`` outcome.
        """
        package_id = identifier(package_id, "package_id")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
            raise ValidationError("expected_hash must be a SHA-256 hexadecimal string.")
        digest = expected_hash.lower()
        package = self.service.guest_package_list(vm_name, package_id)["package"]
        if not package.get("present") or not package.get("manifest_present"):
            return _unverified(package_id, "The package is not published on the guest.")
        try:
            manifest = base64.b64decode(str(package.get("manifest_base64") or ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError("The published manifest is invalid.") from exc
        try:
            verdict = verify_staged(
                package_id=package_id, expected_sha256=digest, manifest=manifest,
                listing=list(package.get("files") or []), label="published",
            )
        except ValidationError as exc:
            return _unverified(package_id, str(exc))
        return {
            "package_id": package_id, "verified": True,
            "files_verified": verdict["files_verified"], "manifest_sha256": digest,
            "payload_executed": False,
        }

    def collect(
        self, vm_name: str, package_id: str, run_id: str, output: Path,
        *, include_kernel_dump: bool = False, chunk_bytes: int = CHUNK_BYTES,
    ) -> dict[str, Any]:
        package_id = identifier(package_id, "package_id")
        run_id = identifier(run_id, "run_id")
        arguments = [f"-PackageId '{package_id}'", f"-RunId '{run_id}'"]
        if include_kernel_dump:
            # `-File` hands a script its arguments as text, so `-Switch:$false`
            # arrives as the string "$false" and fails to bind. The switch is
            # either absent or present.
            arguments.append("-IncludeKernelDump")
        result = self.service.guest_exec(
            vm_name,
            guest_script(rf"{GUEST_ROOT}\Tools\New-DriverLabEvidence.ps1", *arguments),
        )
        execution = result.get("execution", {})
        if not execution.get("success"):
            raise ValidationError(
                f"Guest evidence collection failed: {failure_detail(execution)}"
            )
        evidence_index = rf"{GUEST_ROOT}\Evidence\{run_id}\evidence.json"
        downloaded = self.service.guest_read_file(vm_name, evidence_index, MAX_MANIFEST_BYTES)
        raw = base64.b64decode(downloaded["file"]["content_base64"], validate=True)
        index = json.loads(raw.decode("utf-8"))
        if index.get("schema") != 1 or not isinstance(index.get("files"), list):
            raise ValidationError("Guest evidence index is invalid.")
        if len(index["files"]) > 1024:
            raise ValidationError("Guest evidence index contains too many files.")
        destination = output.resolve() / run_id
        destination.mkdir(parents=True, exist_ok=False)
        receipts = []
        for entry in index["files"]:
            relative = entry_path(entry)
            size = entry.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_PACKAGE_BYTES:
                raise ValidationError("Guest evidence file size is invalid.")
            expected = entry.get("sha256", "")
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValidationError("Guest evidence hash is invalid.")
            remote = rf"{GUEST_ROOT}\Evidence\{run_id}" + "\\" + "\\".join(PurePosixPath(relative).parts)
            local = destination.joinpath(*PurePosixPath(relative).parts)
            self._download(vm_name, remote, local, size, expected, chunk_bytes)
            receipts.append({"path": relative, "size": size, "sha256": expected})
        receipt = {
            "schema": 1, "package_id": package_id, "run_id": run_id,
            "files": receipts, "payload_executed": False,
        }
        (destination / "host-receipt.json").write_bytes(canonical_json(receipt))
        return receipt

    def _download(
        self, vm_name: str, remote: str, local: Path, size: int, expected: str, chunk_bytes: int
    ) -> None:
        if not 1024 * 1024 <= chunk_bytes <= 8 * 1024 * 1024:
            raise ValidationError("chunk_bytes must be between 1 MiB and 8 MiB.")
        local.parent.mkdir(parents=True, exist_ok=True)
        temporary = local.with_name(local.name + f".{uuid.uuid4().hex}.tmp")
        digest = hashlib.sha256()
        try:
            with temporary.open("xb") as stream:
                for offset in range(0, size, chunk_bytes):
                    count = min(chunk_bytes, size - offset)
                    result = self.service.guest_read_chunk(vm_name, remote, offset, count)
                    chunk = result["chunk"]
                    data = base64.b64decode(chunk["content_base64"], validate=True)
                    if len(data) != count or chunk["sha256"] != hashlib.sha256(data).hexdigest():
                        raise ValidationError(f"Evidence chunk failed validation: {remote}")
                    stream.write(data)
                    digest.update(data)
                stream.flush()
                os.fsync(stream.fileno())
            if digest.hexdigest() != expected:
                raise ValidationError(f"Evidence SHA-256 mismatch: {remote}")
            temporary.replace(local)
        finally:
            temporary.unlink(missing_ok=True)
