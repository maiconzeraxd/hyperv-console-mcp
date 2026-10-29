"""Live check of large guest payloads over both transports.

Only for a disposable guest. Runs a ~100 KB ``guest_exec`` command and a
~100 KB ``guest_put_file`` over PowerShell Direct and over SSH, reads the file
back and compares the digest, then removes it from the guest. Nothing here runs
in the default pytest suite.

    set HYPERV_MCP_LIVE_TEST_VM=LAB-Disposable
    set HYPERV_MCP_GUEST_USERNAME=lab
    set HYPERV_MCP_GUEST_PASSWORD=...
    python -I tests/live_large_payload.py --provision-ssh --deprovision
"""

from __future__ import annotations

import argparse
import hashlib
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

PAYLOAD_BYTES = 100 * 1024
GUEST_DIRECTORY = r"C:\ProgramData\HyperVConsoleMCP"
MARKER = "HVMCP-LARGE-OK"


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


def make_service(vm: str, transport: str) -> tuple[HyperVService, GuestSplitBackend]:
    settings = Settings.from_env({
        **os.environ,
        "HYPERV_MCP_ALLOWED_VMS": vm,
        "HYPERV_MCP_ALLOW_GUEST": "true",
        "HYPERV_MCP_GUEST_TRANSPORT": transport,
    })
    backend = GuestSplitBackend()
    return HyperVService(backend, settings), backend


def large_command() -> str:
    """A command far past the 32 767-character command line, and verifiable."""
    padding = "".join(f"# payload line {index:05d} {'x' * 40}\n" for index in range(1600))
    return f"{padding}'{MARKER}'\n"


def payload_bytes() -> bytes:
    block = b"HVMCP large payload line %06d %s\n"
    chunks = []
    size = 0
    index = 0
    while size < PAYLOAD_BYTES:
        chunk = block % (index, hashlib.sha256(str(index).encode()).hexdigest().encode())
        chunks.append(chunk)
        size += len(chunk)
        index += 1
    return b"".join(chunks)[:PAYLOAD_BYTES]


def exercise(service: HyperVService, vm: str, transport: str, work: Path) -> int:
    """Run the two large-payload calls and return a failure count."""
    failures = 0
    work.mkdir(parents=True, exist_ok=True)
    command = large_command()
    executed = report(
        f"guest_exec {len(command)} chars over {transport}",
        lambda: service.guest_exec(vm, command),
    )
    execution = executed.get("execution") or {}
    if executed.get("error") or MARKER not in str(execution.get("output", "")):
        failures += 1

    data = payload_bytes()
    source = work / f"payload-{transport}.bin"
    source.write_bytes(data)
    guest_path = rf"{GUEST_DIRECTORY}\large-payload-{transport}.bin"
    uploaded = report(
        f"guest_put_file {len(data)} bytes over {transport}",
        lambda: service.guest_put_file(
            vm, str(source), guest_path, overwrite=True, confirm_path=guest_path,
        ),
    )
    if uploaded.get("error"):
        failures += 1

    back = work / f"payload-{transport}-back.bin"
    downloaded = report(
        f"guest_get_file {len(data)} bytes over {transport}",
        lambda: service.guest_get_file(
            vm, guest_path, str(back), overwrite=True, confirm_path=str(back),
        ),
    )
    if downloaded.get("error"):
        failures += 1
    elif back.exists() and hashlib.sha256(back.read_bytes()).hexdigest() != hashlib.sha256(data).hexdigest():
        print(json.dumps({"step": f"digest {transport}", "result": "MISMATCH"}))
        failures += 1

    report(
        f"cleanup over {transport}",
        lambda: service.guest_exec(vm, f"Remove-Item -LiteralPath '{guest_path}' -Force -ErrorAction SilentlyContinue"),
    )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", default=os.environ.get("HYPERV_MCP_LIVE_TEST_VM", ""))
    parser.add_argument("--use-existing-adapter", action="store_true")
    parser.add_argument("--provision-ssh", action="store_true")
    parser.add_argument("--deprovision", action="store_true")
    args = parser.parse_args()
    if not args.vm:
        print("Set HYPERV_MCP_LIVE_TEST_VM or pass --vm to a disposable VM.", file=sys.stderr)
        return 2

    failures = 0
    direct, direct_backend = make_service(args.vm, "psdirect")
    ssh: HyperVService | None = None
    ssh_backend: GuestSplitBackend | None = None
    try:
        failures += exercise(direct, args.vm, "psdirect", Path(direct.settings.transfer_root))

        if args.provision_ssh:
            provisioned = report(
                "guest_ssh_provision",
                lambda: direct.guest_ssh_provision(
                    args.vm, args.vm, use_existing_adapter=args.use_existing_adapter,
                ),
            )
            ssh_ready = not provisioned.get("error") and bool(provisioned.get("verified"))
            if not ssh_ready:
                failures += 1
        else:
            ssh_ready = True

        if ssh_ready:
            ssh, ssh_backend = make_service(args.vm, "ssh")
            failures += exercise(ssh, args.vm, "ssh", Path(ssh.settings.transfer_root))

        if args.deprovision:
            removed = report(
                "guest_ssh_deprovision",
                lambda: direct.guest_ssh_deprovision(args.vm, args.vm),
            )
            if removed.get("error") or not removed.get("state_removed"):
                failures += 1
    finally:
        direct_backend.close()
        if ssh_backend is not None:
            ssh_backend.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
