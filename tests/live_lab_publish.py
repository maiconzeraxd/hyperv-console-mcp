"""Explicit live check for atomic package publishing. Requires a disposable VM.

Run with the same environment the server uses (allowlist, guest credentials, lab
roots), then:

    python tests/live_lab_publish.py --vm <disposable-vm>
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hyperv_console_mcp.backend import PowerShellBackend
from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import HyperVError
from hyperv_console_mcp.lab.cli import initialize
from hyperv_console_mcp.lab.transfer import (
    DriverLabTransfer,
    build_manifest,
    canonical_json,
    manifest_hash,
)
from hyperv_console_mcp.service import HyperVService


def _write(
    service: HyperVService, vm: str, path: str, data: bytes, *, overwrite: bool = False
) -> None:
    service.guest_write_file(
        vm,
        path,
        base64.b64encode(data).decode("ascii"),
        create_parent=True,
        overwrite=overwrite,
        confirm_path=path if overwrite else None,
    )


def _guest_path_exists(service: HyperVService, vm: str, path: str) -> bool:
    result = service.guest_exec(vm, f"Test-Path -LiteralPath '{path}' -PathType Container")
    return "True" in str(result["execution"]["output"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", default=os.environ.get("HYPERV_MCP_LIVE_TEST_VM", ""))
    args = parser.parse_args()
    if not args.vm:
        print("Set HYPERV_MCP_LIVE_TEST_VM or pass --vm to a disposable VM.", file=sys.stderr)
        return 2
    backend = PowerShellBackend()
    service = HyperVService(backend, Settings.from_env())
    transfer = DriverLabTransfer(service)
    failures = 0
    try:
        print(json.dumps({"initialize": initialize(service, args.vm)}, indent=2))
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "package"
            source.mkdir()
            (source / "Run-Lab.ps1").write_text(
                "param($RunId, $PackageId) Write-Output \"noop $RunId\"\n", encoding="utf-8"
            )
            (source / "driver.sys").write_bytes(b"not a real driver")
            package_id = "live." + uuid.uuid4().hex[:8]
            manifest = build_manifest(source, package_id)
            staged = transfer.stage(args.vm, source, manifest)
            print(json.dumps({"stage": staged}, indent=2))
            verified = transfer.verify(args.vm, package_id, staged["manifest_sha256"])
            print(json.dumps({"verify": verified}, indent=2))
            if not verified["verified"]:
                failures += 1

            # Verification must also refuse a published package that changed after
            # it was approved, and name the file. The guest only reports facts, so
            # this exercises the host-side comparison, not a guest verdict.
            _write(
                service, args.vm, rf"{staged['guest_path']}\driver.sys", b"swapped",
                overwrite=True,
            )
            tampered = transfer.verify(args.vm, package_id, staged["manifest_sha256"])
            print(json.dumps({"verify_tampered": tampered}, indent=2))
            if tampered["verified"] or "driver.sys" not in str(tampered.get("reason", "")):
                failures += 1

        # A staging directory holding a file the manifest does not describe must never
        # reach Packages: the publish step has to refuse it and leave nothing behind.
        rejected_id = "live." + uuid.uuid4().hex[:8]
        transfer_id = uuid.uuid4().hex
        staging = rf"C:\DriverLab\Transfers\{transfer_id}\{rejected_id}"
        payload = b"binary"
        rejected_manifest = {
            "schema": 1, "package_id": rejected_id,
            "files": [{
                "path": "driver.sys", "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }],
        }
        _write(service, args.vm, staging + r"\driver.sys", payload)
        _write(service, args.vm, staging + r"\extra.dll", b"unverified")
        _write(service, args.vm, staging + r"\lab-manifest.json", canonical_json(rejected_manifest))
        try:
            service.guest_publish_package(
                args.vm, rejected_id, transfer_id, manifest_hash(rejected_manifest)
            )
        except HyperVError as exc:
            print(json.dumps({"publish_rejected": exc.code, "message": str(exc)}, indent=2))
            if "not in the manifest" not in str(exc):
                failures += 1
        else:
            failures += 1
            print(json.dumps({"publish_rejected": False}, indent=2))
        published = _guest_path_exists(service, args.vm, rf"C:\DriverLab\Packages\{rejected_id}")
        print(json.dumps({"rejected_package_absent": not published}, indent=2))
        if published:
            failures += 1
    finally:
        backend.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
