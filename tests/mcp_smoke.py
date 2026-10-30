"""Real MCP stdio smoke test; only read-only or policy-denied live calls.

It starts the server three times: once with the safe profile's default tool
surface, once with the lab profile, and once with every toolset enabled, so the
smoke proves the conversation-sized surface is what each profile promises and
that enabling the rest still exposes the whole server. The host report carries
the registered count per toolset, which is what makes "per toolset" checkable
without starting eleven servers.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# (label, extra environment, expected tool count, expected counts per toolset).
# Keep these in step with the tool table in README.md and with tests/test_server.py.
CONFIGURATIONS = (
    (
        "safe",
        {},
        29,
        {
            "core": 8, "guest": 6, "console": 4, "config": 3,
            "credentials": 2, "media": 3, "ssh": 3,
        },
    ),
    (
        "lab profile",
        {"HYPERV_MCP_PROFILE": "lab"},
        42,
        {"core": 8, "guest": 6, "console": 4, "config": 3, "lab": 10, "kd": 9, "debug": 2},
    ),
    (
        "all toolsets",
        {"HYPERV_MCP_TOOLSETS": "all"},
        55,
        {
            "core": 8, "guest": 6, "console": 4, "config": 3, "credentials": 2,
            "media": 3, "ssh": 3, "serial": 5, "lab": 10, "kd": 9, "debug": 2,
        },
    ),
)


async def check(
    label: str, extra: dict[str, str], expected: int, per_toolset: dict[str, int]
) -> dict:
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
            # The lab profile switches every capability on; the explicit
            # "false" values above still win, so the denied call below stays a
            # policy refusal rather than a power operation on a real VM.
            denied = await session.call_tool(
                "vm_power", {"vm_name": "NORTH-Test", "action": "start"}
            )
            assert denied.is_error
            assert "not_authorized" in denied.content[0].text
            status = await session.call_tool("hyperv_host_status", {})
            assert not status.is_error
            report = status.structured_content
            counts = report["toolsets"]["counts"]
            assert counts == per_toolset, (label, counts)
            assert report["toolsets"]["total"] == expected, (label, counts)
            return {
                "surface": label,
                "mcp_initialize": "ok",
                "tool_count": len(tools.tools),
                "per_toolset": counts,
                "disabled_mutation_blocked": True,
                "live_read_succeeded": True,
            }


async def main() -> None:
    results = [await check(*configuration) for configuration in CONFIGURATIONS]
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
