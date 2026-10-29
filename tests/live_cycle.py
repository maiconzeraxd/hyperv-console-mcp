"""Explicit live autonomous-cycle check. Only for a disposable VM.

Stages a harmless package whose Run-Lab.ps1 only writes a file, then runs the
whole cycle: checkpoint, verify, execute, classify, collect evidence and revert.
This is the end-to-end proof that the lab profile works without a human click.

    set HYPERV_MCP_LIVE_TEST_VM=LAB-Disposable
    set HYPERV_MCP_PROFILE=lab
    python -I tests/live_cycle.py --debugger kdnet

Nothing here runs in the default pytest suite.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hyperv_console_mcp.backend import GuestSplitBackend
from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import HyperVError
from hyperv_console_mcp.lab.transfer import DriverLabTransfer, build_manifest
from hyperv_console_mcp.service import HyperVService

RUNNER = (
    "param($RunId, $PackageId)\n"
    "$dir = Join-Path 'C:\\DriverLab\\Runs' $RunId\n"
    "New-Item -ItemType Directory -Path $dir -Force | Out-Null\n"
    "Set-Content -LiteralPath (Join-Path $dir 'harmless.txt') -Value \"ran $PackageId $RunId\"\n"
    "Write-Output 'harmless package finished'\n"
    "exit 0\n"
)


def report(label: str, action) -> dict:
    try:
        result = action()
    except HyperVError as exc:
        result = {"error": exc.code, "message": str(exc)}
    print(json.dumps({"step": label, "result": result}, ensure_ascii=False, indent=2))
    return result if isinstance(result, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", default=os.environ.get("HYPERV_MCP_LIVE_TEST_VM", ""))
    parser.add_argument("--debugger", choices=("none", "serial", "kdnet"), default="none")
    parser.add_argument("--keep-checkpoint", action="store_true")
    args = parser.parse_args()
    if not args.vm:
        print("Set HYPERV_MCP_LIVE_TEST_VM or pass --vm to a disposable VM.", file=sys.stderr)
        return 2

    settings = Settings.from_env()
    if settings.profile != "lab":
        print("Set HYPERV_MCP_PROFILE=lab: lab_cycle exists only in that profile.", file=sys.stderr)
        return 2

    backend = GuestSplitBackend()
    service = HyperVService(backend, settings)
    transfer = DriverLabTransfer(service)
    package_id = "live." + uuid.uuid4().hex[:8]
    run_id = "run-" + uuid.uuid4().hex[:8]
    failures = 0
    try:
        report("lab_init", lambda: service.lab_init(args.vm, args.vm))
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "package"
            source.mkdir()
            (source / "Run-Lab.ps1").write_text(RUNNER, encoding="utf-8")
            (source / "notes.txt").write_text("harmless lab package\n", encoding="utf-8")
            manifest = build_manifest(source, package_id)
            staged = report("stage", lambda: transfer.stage(args.vm, source, manifest))
            if "manifest_sha256" not in staged:
                return 1
            report("verify", lambda: transfer.verify(
                args.vm, package_id, staged["manifest_sha256"]
            ))
            result = report("lab_cycle", lambda: service.lab_cycle(
                args.vm, package_id, run_id, staged["manifest_sha256"],
                timeout_seconds=300,
                checkpoint_before=True,
                revert_after=not args.keep_checkpoint,
                include_kernel_dump=False,
                debugger=args.debugger,
            ))
        if result.get("outcome") != "ok":
            failures += 1
            print(json.dumps({"outcome": result.get("outcome"),
                              "detail": result.get("detail")}, indent=2))
    finally:
        backend.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
