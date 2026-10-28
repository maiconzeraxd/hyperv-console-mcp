"""Explicit live read-only checks. Never starts, stops, or modifies a VM."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hyperv_console_mcp.backend import PowerShellBackend
from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import HyperVError
from hyperv_console_mcp.service import HyperVService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", default="NORTH-Test")
    args = parser.parse_args()
    backend = PowerShellBackend()
    service = HyperVService(backend, Settings(allowed_vms=frozenset({args.vm})))
    try:
        for label, operation in (
            ("host_status", service.host_status),
            ("vm_get", lambda: service.get_vm(args.vm)),
            ("console_info", lambda: service.console_info(args.vm)),
            ("debug_serial_status", lambda: service.debug_serial_status(args.vm)),
        ):
            start = time.perf_counter()
            try:
                result = operation()
            except HyperVError as exc:
                result = {"error": exc.code, "message": str(exc)}
            print(json.dumps({
                "operation": label,
                "elapsed_seconds": round(time.perf_counter() - start, 3),
                "result": result,
            }, ensure_ascii=False, indent=2))
    finally:
        backend.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
