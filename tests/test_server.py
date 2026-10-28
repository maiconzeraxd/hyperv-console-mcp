from __future__ import annotations

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent, TextContent

from hyperv_console_mcp.capture import Screenshot
from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import PolicyError
from hyperv_console_mcp.server import create_server
from hyperv_console_mcp.service import HyperVService

from test_service import FakeBackend


@pytest.fixture
def server() -> tuple[object, FakeBackend]:
    backend = FakeBackend()
    service = HyperVService(
        backend,
        Settings(
            allowed_vms=frozenset({"NORTH-Test"}),
            allow_power=True,
            allow_screenshots=True,
        ),
    )
    return create_server(service), backend


def test_all_tools_are_discoverable(server) -> None:
    mcp, _ = server
    tools = {tool.name: tool for tool in anyio.run(mcp.list_tools)}
    assert len(tools) == 21
    assert tools["vm_list"].input_schema["type"] == "object"
    assert tools["vm_list"].input_schema.get("required", []) == []
    assert "password" not in tools["guest_exec"].input_schema["properties"]
    assert tools["vm_list"].annotations.read_only_hint is True
    assert tools["checkpoint_restore"].annotations.destructive_hint is True


def test_tool_calls_service_and_returns_structured_output(server) -> None:
    mcp, backend = server
    result = anyio.run(
        mcp.call_tool, "vm_power", {"vm_name": "NORTH-Test", "action": "start"}
    )
    assert result.is_error is False
    assert result.structured_content["operation"] == "vm_power"
    assert backend.calls[-1][0] == "vm_power"


def test_tool_error_does_not_expose_traceback(server) -> None:
    mcp, _ = server
    with pytest.raises(ToolError, match="not_authorized"):
        anyio.run(mcp.call_tool, "guest_exec", {"vm_name": "NORTH-Test", "command": "hostname"})


def test_screenshot_returns_metadata_and_png(server) -> None:
    mcp, _ = server

    class FakeCapture:
        def capture(self, vm_name: str, *, region: str, max_width: int) -> Screenshot:
            return Screenshot(b"fake png", {"vm": vm_name, "experimental": True})

    service = HyperVService(
        FakeBackend(),
        Settings(allowed_vms=frozenset({"NORTH-Test"}), allow_screenshots=True),
    )
    server_with_capture = create_server(service, FakeCapture())
    result = anyio.run(
        server_with_capture.call_tool,
        "console_screenshot",
        {"vm_name": "NORTH-Test", "region": "client"},
    )
    assert isinstance(result.content[0], TextContent)
    assert isinstance(result.content[1], ImageContent)
    assert result.structured_content["experimental"] is True
