"""Explicit live check for guest automation. Only for a disposable VM.

Configures autologon and the no-prompt toggles, and optionally captures the
console thumbnail so an operator can see the guest reached its desktop without
anyone typing. ``--revert`` puts every recorded setting back.

    set HYPERV_MCP_LIVE_TEST_VM=LAB-Disposable
    python -I tests/live_guest_automation.py --configure --restart
    python -I tests/live_guest_automation.py --check --save desktop.png
    python -I tests/live_guest_automation.py --revert

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
    parser.add_argument("--configure", action="store_true", help="Enable autologon and the toggles.")
    parser.add_argument("--restart", action="store_true", help="Reboot after configuring.")
    parser.add_argument("--check", action="store_true", help="Capture the console thumbnail.")
    parser.add_argument("--revert", action="store_true", help="Restore the recorded settings.")
    parser.add_argument("--save", default="", help="Write the screenshot to this path.")
    args = parser.parse_args()
    if not args.vm:
        print("Set HYPERV_MCP_LIVE_TEST_VM or pass --vm to a disposable VM.", file=sys.stderr)
        return 2

    backend = PowerShellBackend()
    service = HyperVService(backend, Settings.from_env())
    failures = 0
    try:
        if args.configure:
            configured = report("guest_autologon_configure", lambda: (
                service.guest_autologon_configure(
                    args.vm, True, args.vm,
                    restart=args.restart, lock_screen=False, disable_cad=True,
                    power=True, uac=False, windows_update=False,
                )
            ))
            if configured.get("error"):
                failures += 1
        if args.check:
            status = report("guest_config_status", lambda: service.guest_config_status(args.vm))
            if status.get("error"):
                failures += 1
            try:
                shot = service.capture_thumbnail(args.vm)
            except HyperVError as exc:
                print(json.dumps({"step": "console_screenshot", "error": exc.code,
                                  "message": str(exc)}, indent=2))
                failures += 1
            else:
                target = Path(args.save or f"{args.vm}-desktop.png")
                target.write_bytes(shot.png)
                print(json.dumps({"step": "console_screenshot", "path": str(target),
                                  **shot.metadata}, indent=2, default=str))
        if args.revert:
            reverted = report("guest_config_revert", lambda: service.guest_config_revert(
                args.vm, args.vm, restart=args.restart
            ))
            if reverted.get("error"):
                failures += 1
    finally:
        backend.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
