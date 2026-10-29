"""Real MCP stdio smoke test; only read-only or policy-denied live calls."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update(
        HYPERV_MCP_ALLOWED_VMS="NORTH-Test",
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
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            # Keep this in step with the tool table in README.md: the smoke
            # proves the stdio server really exposes the whole surface.
            assert len(tools.tools) == 78, len(tools.tools)
            denied = await session.call_tool(
                "vm_power", {"vm_name": "NORTH-Test", "action": "start"}
            )
            assert denied.is_error
            assert "not_authorized" in denied.content[0].text
            status = await session.call_tool("hyperv_host_status", {})
            print(json.dumps({
                "mcp_initialize": "ok",
                "tool_count": len(tools.tools),
                "disabled_mutation_blocked": True,
                "live_read_succeeded": not status.is_error,
                "live_read_result": status.content[0].text,
            }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
