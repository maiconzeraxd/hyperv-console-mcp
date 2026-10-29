"""Explicit live kernel-debugging check. Only for a disposable VM.

Reads the VM from HYPERV_MCP_LIVE_TEST_VM (or --vm). Over KDNET it prepares the
host and guest side first unless --skip-prepare is given; the guest then has to
reboot before the setting takes effect, so the first run normally stops there.
Over serial it uses a COM1 debugger pipe that debug_serial_prepare already set
up and a Running VM.

    set HYPERV_MCP_LIVE_TEST_VM=LAB-Disposable
    set HYPERV_MCP_ALLOW_DEBUGGER=true
    python -I tests/live_kd.py --transport kdnet --deprovision

Nothing here runs in the default pytest suite.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hyperv_console_mcp.backend import PowerShellBackend
from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import HyperVError
from hyperv_console_mcp.service import HyperVService


def report(label: str, action) -> dict:
    start = time.perf_counter()
    try:
        result = action()
    except HyperVError as exc:
        result = {"error": exc.code, "message": str(exc)}
    print(json.dumps({
        "step": label,
        "elapsed_seconds": round(time.perf_counter() - start, 3),
        "result": result,
    }, ensure_ascii=False, indent=2))
    return result if isinstance(result, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", default=os.environ.get("HYPERV_MCP_LIVE_TEST_VM", ""))
    parser.add_argument("--transport", choices=("kdnet", "serial"), default="kdnet")
    parser.add_argument("--skip-prepare", action="store_true")
    parser.add_argument("--deprovision", action="store_true")
    parser.add_argument("--go", action="store_true", help="Also resume the target after the stack.")
    args = parser.parse_args()
    if not args.vm:
        print("Set HYPERV_MCP_LIVE_TEST_VM or pass --vm to a disposable VM.", file=sys.stderr)
        return 2

    backend = PowerShellBackend()
    service = HyperVService(backend, Settings.from_env())
    failures = 0
    try:
        if args.transport == "kdnet" and not args.skip_prepare:
            status = service.debug_kdnet_status(args.vm)
            print(json.dumps({"kdnet_status": status}, indent=2))
            if not status.get("prepared"):
                report("debug_kdnet_prepare", lambda: service.debug_kdnet_prepare(args.vm, args.vm))
                print("The guest must reboot before KDNET takes effect. "
                      "Reboot it, then rerun with --skip-prepare.")
                return 0

        attached = report("kd_attach", lambda: service.kd_attach(args.vm, args.transport))
        if not attached.get("attached"):
            return 1
        report("kd_status", lambda: service.kd_status(args.vm))
        broken = report("kd_break", lambda: service.kd_break(args.vm))
        if broken.get("error"):
            failures += 1
        report("kd_stack", lambda: service.kd_stack(args.vm, 12))
        report("kd_command", lambda: service.kd_command(args.vm, "lm"))
        if args.go:
            # A resume only returns when the target breaks again, so a timeout
            # here is an expected outcome rather than a failure.
            report("kd_go", lambda: service.kd_go(args.vm, timeout_ms=15_000))
        report("kd_detach", lambda: service.kd_detach(args.vm))
    finally:
        if args.deprovision and args.transport == "kdnet":
            report("debug_kdnet_deprovision",
                   lambda: service.debug_kdnet_deprovision(args.vm, args.vm))
        backend.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
