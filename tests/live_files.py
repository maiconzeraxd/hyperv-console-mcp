"""Live check of the file manager, both directions, on a disposable guest.

Creates a folder on each side, copies a file from the host into it, lists the
guest directory to see the file, copies it back and compares the digests, then
renames and deletes on both sides. The progress events the transfer publishes
are printed as they arrive, because "progresso visível" is half of what this
wave promised.

Only for a disposable guest. Nothing here runs in the default pytest suite.

    set HYPERV_MCP_LIVE_TEST_VM=LAB-Disposable
    python -I tests/live_files.py

The guest credential comes from the Windows Credential Manager for that VM, or
from HYPERV_MCP_GUEST_USERNAME/HYPERV_MCP_GUEST_PASSWORD. The host side works
in a fresh temporary directory, which is used as the transfer root, so nothing
outside it is touched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hyperv_console_mcp.backend import GuestSplitBackend
from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import HyperVError
from hyperv_console_mcp.events import EVENTS
from hyperv_console_mcp.service import HyperVService

GUEST_DIRECTORY = r"C:\DriverLab"
MARKER = b"HVMCP-FILE-MANAGER-OK"


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


def make_service(vm: str, transport: str, work: Path) -> HyperVService:
    settings = Settings.from_env({
        **os.environ,
        "HYPERV_MCP_ALLOWED_VMS": vm,
        "HYPERV_MCP_ALLOW_GUEST": "true",
        "HYPERV_MCP_GUEST_TRANSPORT": transport,
        "HYPERV_MCP_TRANSFER_ROOT": str(work),
        "HYPERV_MCP_LAB_ROOT": str(work / "lab"),
        "HYPERV_MCP_EVIDENCE_ROOT": str(work / "evidence"),
        "HYPERV_MCP_ISO_ROOT": str(work / "media"),
    })
    return HyperVService(GuestSplitBackend(), settings)


def progress_events(after: int) -> list[dict]:
    return [
        event for event in EVENTS.snapshot(after)["events"]
        if event["phase"] == "progress"
        and event["operation"] in {"guest_put_file", "guest_get_file"}
    ]


def exercise(service: HyperVService, vm: str, work: Path, stamp: str) -> int:
    """Run the wave's definition of done and return a failure count."""
    failures = 0
    for name in ("lab", "evidence", "media"):
        (work / name).mkdir(parents=True, exist_ok=True)

    roots = report("file_roots", service.file_roots)
    if [root["name"] for root in roots.get("roots", [])] != [
        "lab", "evidence", "transfer", "media"
    ]:
        failures += 1

    host_folder = f"live-{stamp}"
    made_host = report(
        f"host_make_dir {host_folder}",
        lambda: service.host_make_dir("transfer", host_folder),
    )
    if not (made_host.get("directory") or {}).get("created"):
        failures += 1

    payload = MARKER + bytes(range(256)) * 8
    source = work / host_folder / "driver.sys"
    source.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    guest_folder = rf"{GUEST_DIRECTORY}\live-{stamp}"
    made_guest = report(
        f"guest_make_dir {guest_folder}",
        lambda: service.guest_make_dir(vm, guest_folder),
    )
    if made_guest.get("error"):
        failures += 1

    before = EVENTS.snapshot()["latest_sequence"]
    guest_path = rf"{guest_folder}\driver.sys"
    uploaded = report(
        f"guest_put_file {len(payload)} bytes",
        lambda: service.guest_put_file(
            vm, str(source), guest_path, overwrite=True, confirm_path=guest_path,
        ),
    )
    file = uploaded.get("file") or {}
    if file.get("sha256") != expected:
        print(json.dumps({"step": "upload digest", "result": file.get("sha256"), "expected": expected}))
        failures += 1
    events = progress_events(before)
    print(json.dumps({
        "step": "progress events on the upload",
        "count": len(events),
        "percents": [event["progress"] for event in events],
    }))

    listed = report(
        f"guest_list_dir {guest_folder}",
        lambda: service.guest_list_dir(vm, guest_folder),
    )
    names = [entry["name"] for entry in (listed.get("directory") or {}).get("entries", [])]
    if "driver.sys" not in names:
        failures += 1

    collected = work / host_folder / "driver-back.sys"
    before = EVENTS.snapshot()["latest_sequence"]
    downloaded = report(
        f"guest_get_file {len(payload)} bytes",
        lambda: service.guest_get_file(
            vm, guest_path, str(collected), overwrite=True, confirm_path=str(collected),
        ),
    )
    if (downloaded.get("file") or {}).get("sha256") != expected:
        failures += 1
    if not collected.is_file() or hashlib.sha256(collected.read_bytes()).hexdigest() != expected:
        print(json.dumps({"step": "round trip", "result": "MISMATCH"}))
        failures += 1
    events = progress_events(before)
    print(json.dumps({
        "step": "progress events on the download",
        "count": len(events),
        "percents": [event["progress"] for event in events],
    }))

    renamed = report(
        "guest_rename driver.sys -> driver-old.sys",
        lambda: service.guest_rename(vm, guest_path, "driver-old.sys"),
    )
    if not (renamed.get("entry") or {}).get("path"):
        failures += 1

    refused = report(
        "guest_delete without confirm_path",
        lambda: service.guest_delete(vm, guest_folder, guest_folder + "-wrong", recursive=True),
    )
    if not refused.get("error"):
        failures += 1

    report(
        f"guest_delete {guest_folder}",
        lambda: service.guest_delete(
            vm, guest_folder, guest_folder, recursive=True,
        ),
    )
    report(
        f"host_delete {host_folder}",
        lambda: service.host_delete(
            "transfer", host_folder, host_folder, recursive=True,
        ),
    )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", default=os.environ.get("HYPERV_MCP_LIVE_TEST_VM", ""))
    parser.add_argument("--transport", default="psdirect", choices=("psdirect", "ssh"))
    args = parser.parse_args()
    if not args.vm:
        print("Set HYPERV_MCP_LIVE_TEST_VM or pass --vm to a disposable VM.", file=sys.stderr)
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    with tempfile.TemporaryDirectory(prefix="hvmcp-live-files-") as scratch:
        work = Path(scratch)
        service = make_service(args.vm, args.transport, work)
        failures = exercise(service, args.vm, work, stamp)

    print(json.dumps({"vm": args.vm, "transport": args.transport, "failures": failures}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
