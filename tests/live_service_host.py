"""Live proof of the split: an MCP client, a host in another process, one pipe.

The default smoke runs the server with a private service, because a shared host
would outlive it and keep the smoke's policy. This script is the other half: it
starts a real `hyperv-service` on a pipe of its own, points a real MCP stdio
client at that pipe, and checks that a read crosses the boundary and that the
operation events the tool produced are visible to a second client — which is the
bug the shared service exists to fix.

Read-only: host status, VM inventory and one VM read. Nothing is mutated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from hyperv_console_mcp.service.host import ServiceClient
from hyperv_console_mcp.service.remote import ServiceProxy

START_TIMEOUT_SECONDS = 30.0


def start_host(root: Path, pipe: str) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-m", "hyperv_console_mcp.service.host", "--pipe", pipe],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    client = ServiceClient(pipe)
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if client.available():
            return process
        if process.poll() is not None:
            raise SystemExit(f"hyperv-service exited: {process.stdout.read()}")
        time.sleep(0.2)
    process.terminate()
    raise SystemExit("hyperv-service did not answer in time.")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", default=os.environ.get("HYPERV_MCP_LIVE_TEST_VM", ""))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.vm:
        parser.error("name a disposable VM with --vm or HYPERV_MCP_LIVE_TEST_VM")

    root = Path(__file__).resolve().parents[1]
    pipe = rf"\\.\pipe\HyperVConsoleMCP-Service-live-{uuid.uuid4().hex}"
    records: list[dict] = []
    failures = 0

    def emit(record: dict) -> None:
        records.append(record)
        print(json.dumps(record, ensure_ascii=False, indent=2), flush=True)

    process = start_host(root, pipe)
    try:
        environment = os.environ.copy()
        environment.update(
            HYPERV_MCP_ALLOWED_VMS=args.vm,
            HYPERV_MCP_SERVICE_PIPE=pipe,
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-I", str(root / "scripts" / "run_server.py")],
            env=environment,
            cwd=root,
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                emit({"mcp_initialize": "ok", "tool_count": len(tools.tools), "pipe": pipe})
                for name, arguments in (
                    ("hyperv_host_status", {}),
                    ("vm_list", {}),
                    ("vm_get", {"vm_name": args.vm}),
                ):
                    started = time.perf_counter()
                    response = await session.call_tool(name, arguments)
                    record = {
                        "operation": name,
                        "elapsed_seconds": round(time.perf_counter() - started, 3),
                        "success": not response.is_error,
                    }
                    if response.is_error:
                        text = response.content[0].text
                        record["error"] = text
                        # Hyper-V refuses an unelevated shell, which says nothing
                        # about the split this script exists to prove.
                        if "access_denied" in text:
                            record["skipped"] = "Hyper-V refused the read; run elevated"
                        else:
                            failures += 1
                    else:
                        record["result"] = response.structured_content
                    emit(record)

        # A second client sees what the first one did: the events live with the
        # host, not inside whichever client happened to make the call.
        snapshot = ServiceProxy(ServiceClient(pipe)).event_snapshot()
        operations = sorted({event["operation"] for event in snapshot["events"]})
        emit({"second_client_events": operations, "event_count": len(snapshot["events"])})
        if not operations:
            failures += 1
            emit({"error": "the host published no operation events for the calls above"})
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    if args.output:
        args.output.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
