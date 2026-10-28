"""Live MCP client diagnostics. Performs read-only calls; never mutates a VM."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", default="NORTH-Test")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    records = []

    def emit(record: dict) -> None:
        records.append(record)
        print(json.dumps(record, ensure_ascii=False, indent=2), flush=True)
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update(
        HYPERV_MCP_ALLOWED_VMS=args.vm,
        HYPERV_MCP_ALLOW_POWER="false",
        HYPERV_MCP_ALLOW_CHECKPOINTS="false",
        HYPERV_MCP_ALLOW_GUEST="false",
        HYPERV_MCP_ALLOW_CONSOLE="false",
        HYPERV_MCP_ALLOW_SCREENSHOTS="false",
        HYPERV_MCP_ALLOW_DEBUGGER="false",
        HYPERV_MCP_ALLOW_MEDIA="false",
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-I", str(root / "scripts" / "run_server.py")],
        env=environment,
        cwd=root,
    )
    failures = 0
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            emit({"mcp_initialize": "ok", "tool_count": len(tools.tools)})
            calls = (
                ("hyperv_host_status", {}),
                ("vm_get", {"vm_name": args.vm}),
                ("console_info", {"vm_name": args.vm}),
                ("debug_serial_status", {"vm_name": args.vm}),
                ("media_list", {"vm_name": args.vm}),
                ("checkpoint_list", {"vm_name": args.vm}),
            )
            for name, arguments in calls:
                started = time.perf_counter()
                response = await session.call_tool(name, arguments)
                record = {
                    "operation": name,
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "success": not response.is_error,
                }
                if response.is_error:
                    failures += 1
                    record["error"] = response.content[0].text
                else:
                    record["result"] = response.structured_content
                emit(record)
    if args.output:
        args.output.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
