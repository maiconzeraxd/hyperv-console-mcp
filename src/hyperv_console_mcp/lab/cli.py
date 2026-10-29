"""Local CLI and shared implementation for the reproducible Hyper-V driver lab."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from ..config import Settings
from ..errors import HyperVError
from ..hostwmi import create_backend
from ..service import HyperVService
from .transfer import GUEST_ROOT, DriverLabTransfer, build_manifest, guest_script

GUEST_DIRECTORY = Path(__file__).with_name("guest")


def _tool_files() -> list[Path]:
    return sorted(path for path in GUEST_DIRECTORY.iterdir() if path.is_file())


def initialize(service: HyperVService, vm_name: str) -> dict[str, Any]:
    """Install this server's lab tools into the guest.

    A tool whose hash differs from the packaged one is replaced: the server's own
    copy is the authority, and refusing to update would strand every guest on the
    tools it received first, so an upgrade could never reach it. Only a genuine
    bridge failure stops the install.
    """
    verified = 0
    installed = 0
    updated: list[str] = []
    for path in _tool_files():
        content = path.read_bytes()
        remote = rf"C:\DriverLab\Tools\{path.name}"
        encoded = base64.b64encode(content).decode("ascii")
        try:
            existing = service.guest_read_file(vm_name, remote, max_bytes=1_048_576)
        except HyperVError as exc:
            # Only a missing file means "install it". Any other bridge failure has to
            # surface instead of being mistaken for an absent tool.
            if exc.code != "not_found":
                raise
            installed += 1
            service.guest_write_file(
                vm_name, remote, encoded, create_parent=True,
            )
            continue
        if existing["file"]["sha256"].casefold() == hashlib.sha256(content).hexdigest():
            verified += 1
            continue
        updated.append(path.name)
        service.guest_write_file(
            vm_name, remote, encoded, overwrite=True, confirm_path=remote, create_parent=True,
        )
    result = service.guest_exec(
        vm_name, guest_script(rf"{GUEST_ROOT}\Tools\Initialize-DriverLab.ps1")
    )
    return {
        "vm": vm_name,
        "initialization": result,
        "already_verified": verified,
        "installed": installed,
        "updated": updated,
        "payload_executed": False,
    }


def diagnostics(service: HyperVService, vm_name: str) -> dict[str, Any]:
    return service.guest_exec(
        vm_name, guest_script(rf"{GUEST_ROOT}\Tools\Get-DriverLabDiagnostics.ps1")
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hyperv-lab", description=__doc__)
    parser.add_argument("--vm", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init")
    sub.add_parser("diagnostics")
    stage = sub.add_parser("stage")
    stage.add_argument("source", type=Path)
    stage.add_argument("--package-id")
    stage.add_argument("--receipt", type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("package_id")
    verify.add_argument("manifest_sha256")
    checkpoint = sub.add_parser("checkpoint")
    checkpoint.add_argument("name")
    collect = sub.add_parser("collect")
    collect.add_argument("package_id")
    collect.add_argument("run_id")
    collect.add_argument("output", type=Path)
    collect.add_argument("--include-kernel-dump", action="store_true")
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    backend = create_backend()
    service = HyperVService(backend, Settings.from_env())
    try:
        if arguments.action == "init":
            result = initialize(service, arguments.vm)
        elif arguments.action == "diagnostics":
            result = diagnostics(service, arguments.vm)
        elif arguments.action == "stage":
            manifest = build_manifest(arguments.source, arguments.package_id)
            result = DriverLabTransfer(service).stage(arguments.vm, arguments.source, manifest)
            if arguments.receipt:
                arguments.receipt.write_text(json.dumps(result, indent=2), encoding="utf-8")
        elif arguments.action == "verify":
            result = DriverLabTransfer(service).verify(
                arguments.vm, arguments.package_id, arguments.manifest_sha256
            )
        elif arguments.action == "checkpoint":
            result = service.create_checkpoint(arguments.vm, arguments.name)
        elif arguments.action == "collect":
            result = DriverLabTransfer(service).collect(
                arguments.vm, arguments.package_id, arguments.run_id,
                arguments.output, include_kernel_dump=arguments.include_kernel_dump,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except HyperVError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        backend.close()


if __name__ == "__main__":
    main()
