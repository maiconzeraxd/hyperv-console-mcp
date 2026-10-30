"""Live check that the guest session is really persistent.

Four claims, each of which a per-request ``Invoke-Command`` could not satisfy:

  1. a second call lands in the same runspace — a global variable set by the
     first one is still readable;
  2. a file goes up and comes back down with the host's digest intact —
     ``Copy-VMFile`` or ``Copy-Item -ToSession`` on the way up, whichever the
     guest offers, and ``Copy-Item -FromSession`` on the way back;
  3. a guest reboot drops the cached session and the next call opens a new one;
  4. a long guest command does not block a host call, because guest work has
     its own worker.

The reboot is opt-in: it restarts the guest, so it only belongs in a run
against a disposable VM.

    set HYPERV_MCP_LIVE_TEST_VM=LAB-Disposable
    python -I tests/live_guest_session.py --reboot
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hyperv_console_mcp.backend import GuestSplitBackend
from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import HyperVError
from hyperv_console_mcp.service import HyperVService

PAYLOAD_BYTES = 100 * 1024
GUEST_PATH = r"C:\ProgramData\HyperVConsoleMCP\session-probe.bin"
MARKER = "HVMCP-SESSION-OK"


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


def output_of(result: dict) -> str:
    return str((result.get("execution") or {}).get("output", "")).strip()


def make_service(vm: str) -> tuple[HyperVService, GuestSplitBackend]:
    settings = Settings.from_env({
        **os.environ,
        "HYPERV_MCP_ALLOWED_VMS": vm,
        "HYPERV_MCP_ALLOW_GUEST": "true",
        "HYPERV_MCP_GUEST_TRANSPORT": "psdirect",
    })
    backend = GuestSplitBackend()
    return HyperVService(backend, settings), backend


def check_reuse(service: HyperVService, vm: str) -> int:
    first = report(
        "set a global variable",
        lambda: service.guest_exec(
            vm, f"Set-Variable -Name hvmcp_probe -Value '{MARKER}' -Scope Global; 'set'"
        ),
    )
    second = report(
        "read it back",
        lambda: service.guest_exec(
            vm,
            "if (Get-Variable -Name hvmcp_probe -Scope Global -ErrorAction SilentlyContinue) "
            "{ 'REUSED:' + $global:hvmcp_probe } else { 'NEW SESSION' }",
        ),
    )
    if first.get("error") or not output_of(second).startswith("REUSED:"):
        print(json.dumps({"step": "session reuse", "result": "FAILED"}))
        return 1
    return 0


def check_transfer(service: HyperVService, vm: str, work: Path) -> int:
    data = bytes(range(256)) * (PAYLOAD_BYTES // 256)
    digest = hashlib.sha256(data).hexdigest()
    work.mkdir(parents=True, exist_ok=True)
    source = work / "session-probe.bin"
    source.write_bytes(data)
    uploaded = report(
        "guest_put_file",
        lambda: service.guest_put_file(
            vm, str(source), GUEST_PATH, overwrite=True, confirm_path=GUEST_PATH,
        ),
    )
    back = work / "session-probe-back.bin"
    downloaded = report(
        "guest_get_file",
        lambda: service.guest_get_file(
            vm, GUEST_PATH, str(back), overwrite=True, confirm_path=str(back),
        ),
    )
    report(
        "cleanup",
        lambda: service.guest_exec(
            vm, f"Remove-Item -LiteralPath '{GUEST_PATH}' -Force -ErrorAction SilentlyContinue"
        ),
    )
    failures = 0
    if uploaded.get("error") or (uploaded.get("file") or {}).get("sha256") != digest:
        failures += 1
    if downloaded.get("error") or not back.exists():
        failures += 1
    elif hashlib.sha256(back.read_bytes()).hexdigest() != digest:
        print(json.dumps({"step": "transfer digest", "result": "MISMATCH"}))
        failures += 1
    return failures


def check_overlap(service: HyperVService, vm: str, seconds: int = 8) -> int:
    """A guest command that sleeps must not hold up a host call."""
    timings: dict[str, float] = {}

    def slow() -> None:
        start = time.perf_counter()
        report("a slow guest command", lambda: service.guest_exec(
            vm, f"Start-Sleep -Seconds {seconds}; '{MARKER}'"
        ))
        timings["guest"] = round(time.perf_counter() - start, 3)

    worker = threading.Thread(target=slow)
    worker.start()
    time.sleep(2.0)
    started = time.perf_counter()
    state = service.get_vm(vm)
    timings["host_while_guest_busy"] = round(time.perf_counter() - started, 3)
    worker.join()
    print(json.dumps({"step": "overlap", "result": {
        **timings, "vm_state": state["vm"]["state"],
        "blocked": timings["host_while_guest_busy"] >= timings["guest"],
    }}, ensure_ascii=False, indent=2))
    return 0 if timings["host_while_guest_busy"] < timings["guest"] else 1


def wait_for(service: HyperVService, vm: str, *, present: bool, timeout: float) -> dict | None:
    """Poll the guest until it answers (or stops answering) within the deadline."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(3)
        try:
            answer = service.guest_exec(vm, "hostname", timeout_seconds=15)
        except HyperVError:
            if not present:
                return {"answered": False}
            continue
        if present:
            return {"answered": True, "host": output_of(answer)}
    return None


def check_reboot(service: HyperVService, vm: str) -> int:
    """A reboot must invalidate the cached runspace, not wedge it."""
    report("set a variable before the reboot", lambda: service.guest_exec(
        vm, "Set-Variable -Name hvmcp_probe -Value 'before-reboot' -Scope Global; 'set'"
    ))
    report("restart the guest", lambda: service.guest_exec(
        vm, "shutdown /r /t 3 /f; 'restarting'"
    ))
    down = wait_for(service, vm, present=False, timeout=120)
    print(json.dumps({"step": "guest went down", "result": down}, indent=2))
    if down is None:
        print(json.dumps({"step": "reboot", "result": "the guest never stopped answering"}))
        return 1
    back = wait_for(service, vm, present=True, timeout=300)
    print(json.dumps({"step": "guest came back", "result": back}, indent=2))
    if back is None:
        print(json.dumps({"step": "reboot", "result": "the guest never answered again"}))
        return 1
    after = report("read the variable after the reboot", lambda: service.guest_exec(
        vm,
        "if (Get-Variable -Name hvmcp_probe -Scope Global -ErrorAction SilentlyContinue) "
        "{ 'REUSED:' + $global:hvmcp_probe } else { 'NEW SESSION' }",
    ))
    if output_of(after) != "NEW SESSION":
        print(json.dumps({"step": "session after reboot", "result": "FAILED"}))
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", default=os.environ.get("HYPERV_MCP_LIVE_TEST_VM", ""))
    parser.add_argument("--reboot", action="store_true",
                        help="restart the guest to exercise the reconnect path")
    args = parser.parse_args()
    if not args.vm:
        print("Set HYPERV_MCP_LIVE_TEST_VM or pass --vm to a disposable VM.", file=sys.stderr)
        return 2

    service, backend = make_service(args.vm)
    try:
        failures = check_reuse(service, args.vm)
        failures += check_transfer(service, args.vm, Path(service.settings.transfer_root))
        failures += check_overlap(service, args.vm)
        if args.reboot:
            failures += check_reboot(service, args.vm)
    finally:
        backend.close()
    print(json.dumps({"failures": failures}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
