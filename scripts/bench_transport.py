"""Measure guest transport latency and throughput against a disposable VM.

Nothing here is a substitute for a real run: the numbers only mean something on
the machine and VM that produced them, which is why the result carries the host
name, the OS build, the transport name and the date.

    set HYPERV_MCP_LIVE_TEST_VM=LAB-Test
    python scripts/bench_transport.py --vm LAB-Test

The same environment the server uses is required (allowlist, guest credentials,
`HYPERV_MCP_TRANSFER_ROOT`). The VM must be disposable: the benchmark writes and
deletes files under `C:\\DriverLab\\Bench\\` inside the guest.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hyperv_console_mcp.backend import PowerShellBackend
from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import HyperVError
from hyperv_console_mcp.service import HyperVService

GUEST_ROOT = r"C:\DriverLab\Bench"
SMALL_COMMANDS = 10
DEFAULT_SIZES = (1, 100)


def _milliseconds(seconds: float) -> float:
    return round(seconds * 1000, 1)


def _throughput_mib_per_second(size_bytes: int, seconds: float) -> float:
    if seconds <= 0:
        return float("inf")
    return round(size_bytes / (1024 * 1024) / seconds, 2)


def _payload(size_mib: int) -> bytes:
    # A repeating but non-trivial pattern, so compression cannot flatter the result.
    block = bytes(range(256))
    return (block * ((size_mib * 1024 * 1024) // len(block) + 1))[: size_mib * 1024 * 1024]


def measure_commands(service: HyperVService, vm: str, transport: str) -> dict:
    samples = []
    for _ in range(SMALL_COMMANDS):
        started = time.perf_counter()
        result = service.guest_exec(vm, "Write-Output 'benchmark'")
        elapsed = time.perf_counter() - started
        if not result["execution"]["success"]:
            raise HyperVError("The benchmark command failed in the guest.")
        samples.append(elapsed)
    return {
        "transport": transport,
        "workload": f"{SMALL_COMMANDS} small guest commands",
        "count": len(samples),
        "median_ms": _milliseconds(statistics.median(samples)),
        "min_ms": _milliseconds(min(samples)),
        "max_ms": _milliseconds(max(samples)),
    }


def measure_upload(service: HyperVService, vm: str, transport: str, size_mib: int,
                   local: Path) -> dict:
    source = local / f"upload-{size_mib}.bin"
    source.write_bytes(_payload(size_mib))
    remote = f"{GUEST_ROOT}\\upload-{size_mib}.bin"
    started = time.perf_counter()
    result = service.guest_put_file(vm, str(source), remote, overwrite=True,
                                    confirm_path=remote)
    elapsed = time.perf_counter() - started
    return {
        "transport": transport,
        "workload": f"{size_mib} MiB upload",
        "seconds": round(elapsed, 3),
        "mib_per_second": _throughput_mib_per_second(result["file"]["size"], elapsed),
        "sha256": result["file"]["sha256"],
    }


def measure_download(service: HyperVService, vm: str, transport: str, size_mib: int,
                     local: Path) -> dict:
    remote = f"{GUEST_ROOT}\\upload-{size_mib}.bin"
    target = local / f"download-{size_mib}.bin"
    started = time.perf_counter()
    result = service.guest_get_file(vm, remote, str(target), overwrite=True,
                                    confirm_path=str(target))
    elapsed = time.perf_counter() - started
    return {
        "transport": transport,
        "workload": f"{size_mib} MiB download",
        "seconds": round(elapsed, 3),
        "mib_per_second": _throughput_mib_per_second(result["file"]["size"], elapsed),
        "sha256": result["file"]["sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", default=os.environ.get("HYPERV_MCP_LIVE_TEST_VM", ""))
    parser.add_argument("--sizes", default=",".join(str(size) for size in DEFAULT_SIZES),
                        help="Comma-separated MiB sizes for the file transfers.")
    parser.add_argument("--json-out", default="", help="Write the raw result to this path.")
    args = parser.parse_args()
    if not args.vm:
        print("Set HYPERV_MCP_LIVE_TEST_VM or pass --vm to a disposable VM.", file=sys.stderr)
        return 2
    sizes = [int(size) for size in args.sizes.split(",") if size.strip()]

    settings = Settings.from_env()
    backend = PowerShellBackend()
    service = HyperVService(backend, settings)
    results: list[dict] = []
    try:
        transport = service.guest_status(args.vm)["transport"]
        service.guest_exec(args.vm, f"New-Item -ItemType Directory -Force -Path '{GUEST_ROOT}' | Out-Null")
        results.append(measure_commands(service, args.vm, transport))
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary)
            for size in sizes:
                results.append(measure_upload(service, args.vm, transport, size, local))
                results.append(measure_download(service, args.vm, transport, size, local))
        try:
            service.guest_exec(args.vm, f"Remove-Item -LiteralPath '{GUEST_ROOT}' -Recurse -Force")
        except HyperVError as exc:
            print(f"warning: could not clean {GUEST_ROOT}: {exc}", file=sys.stderr)
    finally:
        backend.close()

    report = {
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": platform.node(),
        "platform": platform.platform(),
        "vm": args.vm,
        "results": results,
    }
    print(json.dumps(report, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("\n| workload | transport | median | MiB/s |\n|---|---|---|---|")
    for row in results:
        if "median_ms" in row:
            print(f"| {row['workload']} | {row['transport']} | {row['median_ms']} ms | - |")
        else:
            print(f"| {row['workload']} | {row['transport']} | {row['seconds']} s | "
                  f"{row['mib_per_second']} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
