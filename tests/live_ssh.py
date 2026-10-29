"""Explicit live SSH provisioning check. Only for a disposable guest.

It reads the VM from HYPERV_MCP_LIVE_TEST_VM (or --vm), provisions SSH over
PowerShell Direct, exercises the SSH transport end to end, and optionally
deprovisions again. Nothing here runs in the default pytest suite.

    set HYPERV_MCP_LIVE_TEST_VM=LAB-Disposable
    set HYPERV_MCP_GUEST_USERNAME=lab
    set HYPERV_MCP_GUEST_PASSWORD=...      # only read from the environment
    python -I tests/live_ssh.py --deprovision
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hyperv_console_mcp.backend import GuestSplitBackend
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
    parser.add_argument("--use-existing-adapter", action="store_true")
    parser.add_argument("--deprovision", action="store_true")
    parser.add_argument("--keep", action="store_true", help="Skip deprovisioning.")
    args = parser.parse_args()
    if not args.vm:
        print("Set HYPERV_MCP_LIVE_TEST_VM or pass --vm to a disposable VM.", file=sys.stderr)
        return 2

    settings = Settings.from_env({
        **os.environ,
        "HYPERV_MCP_ALLOWED_VMS": args.vm,
        "HYPERV_MCP_ALLOW_GUEST": "true",
    })
    backend = GuestSplitBackend()
    service = HyperVService(backend, settings)
    failures = 0
    try:
        before = report("guest_ssh_status (before)", lambda: service.guest_ssh_status(args.vm))
        if before.get("error"):
            failures += 1
        provisioned = report(
            "guest_ssh_provision",
            lambda: service.guest_ssh_provision(
                args.vm, args.vm, use_existing_adapter=args.use_existing_adapter
            ),
        )
        if provisioned.get("error"):
            failures += 1
        if provisioned.get("verified"):
            for label, call in (
                ("guest_exec over ssh", lambda: service.guest_exec(args.vm, "hostname")),
                ("guest_status over ssh", lambda: service.guest_status(args.vm)),
            ):
                if report(label, call).get("error"):
                    failures += 1
        after = report("guest_ssh_status (after)", lambda: service.guest_ssh_status(args.vm))
        if after.get("error"):
            failures += 1
        if args.deprovision and not args.keep:
            removed = report(
                "guest_ssh_deprovision",
                lambda: service.guest_ssh_deprovision(args.vm, args.vm),
            )
            if removed.get("error") or not removed.get("state_removed"):
                failures += 1
    finally:
        backend.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
