"""Real MCP stdio smoke test; only read-only or policy-denied live calls.

It starts the server twice: once with the safe profile's default tool surface
and once with every toolset enabled, so the smoke proves both that the
conversation-sized surface is what the profile promises and that enabling the
rest still exposes the whole server.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# (label, extra environment, expected tool count). Keep these in step with the
# tool table in README.md and with tests/test_server.py.
CONFIGURATIONS = (
    ("safe", {}, 29),
    ("all toolsets", {"HYPERV_MCP_TOOLSETS": "all"}, 54),
)


async def check(label: str, extra: dict[str, str], expected: int) -> dict:
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
        # A private service, because the smoke runs with its own policy: a shared
        # host would keep that policy after the smoke exits, and the next window
        # would find itself read-only. tests/live_service_host.py covers the split.
        HYPERV_MCP_SHARED_SERVICE="false",
    )
    environment.update(extra)
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
            assert len(tools.tools) == expected, (label, len(tools.tools))
            denied = await session.call_tool(
                "vm_power", {"vm_name": "NORTH-Test", "action": "start"}
            )
            assert denied.is_error
            assert "not_authorized" in denied.content[0].text
            status = await session.call_tool("hyperv_host_status", {})
            return {
                "surface": label,
                "mcp_initialize": "ok",
                "tool_count": len(tools.tools),
                "disabled_mutation_blocked": True,
                "live_read_succeeded": not status.is_error,
            }


async def main() -> None:
    results = [await check(*configuration) for configuration in CONFIGURATIONS]
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
