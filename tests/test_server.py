from __future__ import annotations

from collections import Counter
from pathlib import Path

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent, TextContent
from test_service import FakeBackend

from hyperv_console_mcp.audit import AuditLogger
from hyperv_console_mcp.capture import Screenshot
from hyperv_console_mcp.config import DEFAULT_TOOLSETS, TOOLSETS, Settings
from hyperv_console_mcp.server import create_server, toolset_for
from hyperv_console_mcp.service import HyperVService


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


def test_the_safe_profile_exposes_the_everyday_surface(server) -> None:
    mcp, _ = server
    tools = {tool.name: tool for tool in anyio.run(mcp.list_tools)}
    # The safe profile registers only its own families — core, guest, console,
    # config, credentials, media and ssh. The debugger, the lab cycle, the serial
    # console and the debug setup are opt-in through HYPERV_MCP_TOOLSETS.
    assert len(tools) == 31
    assert {
        "kd_attach", "kd_status", "kd_inspect", "debug_status", "debug_setup",
        "lab_run", "lab_cycle", "serial_connect", "vm_inject_nmi",
    }.isdisjoint(tools)
    assert tools["vm_list"].input_schema["type"] == "object"
    assert tools["vm_list"].input_schema.get("required", []) == []
    assert "password" not in tools["guest_exec"].input_schema["properties"]
    # Credential tools never accept a secret: rotation generates one on the host.
    assert "password" not in tools["guest_credential"].input_schema["properties"]
    assert "password" not in tools["guest_autologon_configure"].input_schema["properties"]
    assert tools["guest_credential_status"].annotations.read_only_hint is True
    assert tools["guest_config_status"].annotations.read_only_hint is True
    assert tools["guest_report"].annotations.read_only_hint is True
    assert tools["vm_list"].annotations.read_only_hint is True
    assert tools["operation_status"].annotations.read_only_hint is True
    assert tools["checkpoint"].annotations.destructive_hint is True
    assert tools["checkpoint_list"].annotations.read_only_hint is True
    assert set(tools["vm_power"].input_schema["properties"]["action"]["enum"]) == {
        "start", "shutdown", "save", "pause", "resume", "turn_off", "reset"
    }
    assert set(tools["checkpoint"].input_schema["properties"]["action"]["enum"]) == {
        "create", "restore", "delete"
    }
    assert set(tools["guest_report"].input_schema["properties"]["what"]["enum"]) == {
        "status", "processes", "services", "events", "transport"
    }
    assert set(tools["guest_credential"].input_schema["properties"]["action"]["enum"]) == {
        "rotate", "forget"
    }
    assert set(tools["guest_transport"].input_schema["properties"]["transport"]["enum"]) == {
        "auto", "psdirect", "ssh"
    }
    # The switch changes where the next command goes, so it is a mutation.
    assert tools["guest_transport"].annotations.destructive_hint is True
    assert tools["guest_read_file"].input_schema["properties"]["max_bytes"]["default"] == 65_536
    assert set(tools["console_screenshot"].input_schema["properties"]["source"]["enum"]) == {
        "thumbnail", "vmconnect"
    }
    assert tools["console_screenshot"].input_schema["properties"]["source"]["default"] == "thumbnail"
    assert set(tools["console_screenshot"].input_schema["properties"]["region"]["enum"]) == {
        "window", "client"
    }
    assert tools["console_wait_for_change"].annotations.read_only_hint is True
    # One console tool carries every input action as a discriminated union.
    actions = tools["console_input"].input_schema["properties"]["actions"]
    assert actions["type"] == "array"
    assert "text" in str(actions["items"])
    assert tools["console_input"].annotations.destructive_hint is True


def test_enabling_every_toolset_exposes_the_whole_surface() -> None:
    service = HyperVService(
        FakeBackend(),
        Settings(
            allowed_vms=frozenset({"NORTH-Test"}),
            allow_power=True,
            allow_screenshots=True,
            toolsets=TOOLSETS,
        ),
    )
    tools = {tool.name: tool for tool in anyio.run(create_server(service).list_tools)}
    assert len(tools) == 68
    assert tools["lab_run"].annotations.destructive_hint is True
    assert tools["lab_cycle"].annotations.destructive_hint is True
    assert tools["lab_verify"].annotations.read_only_hint is True
    assert "ApproveExecution" not in str(tools["lab_run"].input_schema["properties"])
    assert tools["lab_guest_configure"].input_schema["properties"]["crash_dump"]["default"] is None
    # Kernel debugging: commands mutate the target, analysis only reads it.
    assert tools["kd_command"].annotations.destructive_hint is True
    assert tools["kd_execution"].annotations.destructive_hint is True
    assert tools["kd_breakpoint"].annotations.destructive_hint is True
    assert tools["kd_attach"].annotations.read_only_hint is False
    for name in ("kd_status", "kd_wait_event", "kd_inspect", "debug_status"):
        assert tools[name].annotations.read_only_hint is True, name
    assert set(tools["kd_inspect"].input_schema["properties"]["what"]["enum"]) == {
        "stack", "modules", "symbols", "analyze", "dump"
    }
    # The window shows a per-driver symbol badge, so the same question has to be
    # askable from here; `symbols` is the only action that needs a module name.
    assert "module" in tools["kd_inspect"].input_schema["properties"]
    assert set(tools["kd_attach"].input_schema["properties"]["transport"]["enum"]) == {
        "serial", "kdnet"
    }
    assert set(tools["kd_execution"].input_schema["properties"]["action"]["enum"]) == {
        "break", "go"
    }
    assert set(tools["kd_breakpoint"].input_schema["properties"]["action"]["enum"]) == {
        "set", "clear", "on_load"
    }
    assert set(tools["debug_status"].input_schema["properties"]["transport"]["enum"]) == {
        "serial", "kdnet"
    }
    assert set(tools["debug_setup"].input_schema["properties"]["action"]["enum"]) == {
        "prepare", "guest", "deprovision"
    }
    assert set(tools["lab_cycle"].input_schema["properties"]["debugger"]["enum"]) == {
        "none", "serial", "kdnet"
    }
    assert "key" not in tools["debug_setup"].input_schema["properties"]
    assert "password" not in tools["kd_attach"].input_schema["properties"]


def test_the_host_report_states_the_surface_it_registered() -> None:
    """The counts come from the registration, so the report cannot drift from it."""
    service = HyperVService(
        FakeBackend(),
        Settings(allowed_vms=frozenset({"NORTH-Test"}), toolsets=TOOLSETS),
    )
    result = anyio.run(create_server(service).call_tool, "hyperv_host_status", {})
    assert result.is_error is False
    toolsets = result.structured_content["toolsets"]
    assert toolsets["counts"] == {
        "core": 9, "guest": 7, "console": 4, "config": 3, "credentials": 2,
        "media": 3, "ssh": 3, "serial": 5, "lab": 12, "kd": 9, "debug": 2,
        "files": 9,
    }
    assert toolsets["total"] == 68
    assert toolsets["disabled"] == []


def test_every_tool_belongs_to_a_known_toolset() -> None:
    import ast

    source = Path(__file__).parents[1] / "src" / "hyperv_console_mcp" / "server.py"
    names = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"tool", "image_tool"}
        ):
            continue
        # image_tool takes its name as a keyword; tool takes it first.
        first = node.args[0] if node.args else next(
            (keyword.value for keyword in node.keywords if keyword.arg == "name"), None
        )
        if isinstance(first, ast.Constant):
            names.add(first.value)
    assert len(names) == 68
    assert {toolset_for(name) for name in names} <= TOOLSETS
    # The budget the roadmap asks for is "~40" per profile. `safe` holds it
    # exactly; `lab` is 45 since phase 6 added Driver Verifier and the driver
    # service, and the read side of the verifier lives in `lab_diagnostics`
    # rather than in a second listing tool. `lab_receipts` is one tool with four
    # actions (list, get, compare, export) and `guest_report` one with five
    # (status, processes, services, events, transport) for the same reason: a
    # receipt and a transport probe are reads, and the window needs them without
    # a second implementation of what the answer means. `lab_matrix` is its own
    # tool because it takes a list of VMs where `lab_cycle` takes one, and a
    # shared signature would be a worse lie than an extra schema. The transport
    # switch is its own tool because `guest_report` is read-only and a switch
    # that also read would make that annotation a lie. The file manager is its
    # own opt-in family, so neither default pays for it.
    for profile, limit in (("safe", 40), ("lab", 46)):
        enabled = DEFAULT_TOOLSETS[profile]
        visible = [name for name in names if toolset_for(name) in enabled]
        assert len(visible) <= limit, (profile, len(visible))
    # The counts the host report publishes, and the ones tests/mcp_smoke.py
    # asserts over a real stdio client, come from this same map.
    counts = Counter(toolset_for(name) for name in names)
    assert counts == {
        "core": 9, "guest": 7, "console": 4, "config": 3, "credentials": 2,
        "media": 3, "ssh": 3, "serial": 5, "lab": 12, "kd": 9, "debug": 2,
        "files": 9,
    }
    assert sum(counts.values()) == 68
    assert sum(counts[family] for family in DEFAULT_TOOLSETS["safe"]) == 31
    assert sum(counts[family] for family in DEFAULT_TOOLSETS["lab"]) == 46


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


def test_consolidated_tools_dispatch_to_the_right_operation() -> None:
    backend = FakeBackend()
    service = HyperVService(
        backend,
        Settings(
            allowed_vms=frozenset({"NORTH-Test"}),
            allow_guest=True,
            allow_checkpoints=True,
            allow_debugger=True,
            guest_username="lab",
            guest_password="secret",
            toolsets=TOOLSETS,
        ),
    )
    mcp = create_server(service)
    anyio.run(
        mcp.call_tool,
        "checkpoint",
        {"vm_name": "NORTH-Test", "action": "create", "name": "pre-run"},
    )
    assert backend.calls[-1][0] == "checkpoint_create"
    anyio.run(mcp.call_tool, "guest_report", {"vm_name": "NORTH-Test", "what": "services"})
    assert backend.calls[-1][0] == "guest_services"
    anyio.run(mcp.call_tool, "guest_report", {"vm_name": "NORTH-Test", "what": "events"})
    assert backend.calls[-1][0] == "guest_events"
    anyio.run(
        mcp.call_tool,
        "debug_setup",
        {
            "vm_name": "NORTH-Test", "transport": "serial", "action": "prepare",
            "pipe_name": "north-test-kd", "confirm_vm": "NORTH-Test",
        },
    )
    assert backend.calls[-1][0] == "debug_serial_prepare"


@pytest.mark.parametrize(
    "tool, arguments, message",
    [
        ("checkpoint", {"vm_name": "NORTH-Test", "action": "create"}, "needs name"),
        ("checkpoint", {"vm_name": "NORTH-Test", "action": "restore"}, "needs snapshot_id"),
        (
            "debug_setup",
            {"vm_name": "NORTH-Test", "transport": "serial", "confirm_vm": "NORTH-Test"},
            "needs pipe_name",
        ),
        (
            "debug_setup",
            {
                "vm_name": "NORTH-Test", "transport": "serial", "action": "deprovision",
                "confirm_vm": "NORTH-Test",
            },
            "no deprovision step",
        ),
        (
            "debug_setup",
            {
                "vm_name": "NORTH-Test", "transport": "kdnet", "action": "guest",
                "confirm_vm": "NORTH-Test",
            },
            "part of action='prepare'",
        ),
        ("kd_inspect", {"vm_name": "NORTH-Test", "what": "dump"}, "needs dump_path"),
        ("kd_inspect", {"vm_name": "NORTH-Test", "what": "symbols"}, "needs module"),
    ],
)
def test_a_consolidated_action_requires_its_own_argument(
    tool: str, arguments: dict, message: str
) -> None:
    service = HyperVService(
        FakeBackend(),
        Settings(allowed_vms=frozenset({"NORTH-Test"}), toolsets=TOOLSETS),
    )
    mcp = create_server(service)
    with pytest.raises(ToolError, match=message):
        anyio.run(mcp.call_tool, tool, arguments)


def test_screenshot_returns_metadata_png_and_audit(server, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HYPERV_MCP_EVENT_DIRECTORY", str(tmp_path / "events"))
    mcp, _ = server

    class FakeCapture:
        def capture(self, vm_name: str, *, region: str, max_width: int) -> Screenshot:
            return Screenshot(b"fake png", {"vm": vm_name, "experimental": True})

    service = HyperVService(
        FakeBackend(),
        Settings(allowed_vms=frozenset({"NORTH-Test"}), allow_screenshots=True),
        capture=FakeCapture(),
    )
    server_with_capture = create_server(service)
    service.audit = AuditLogger(tmp_path / "audit.jsonl")
    result = anyio.run(
        server_with_capture.call_tool,
        "console_screenshot",
        {"vm_name": "NORTH-Test", "source": "vmconnect", "region": "client"},
    )
    assert isinstance(result.content[0], TextContent)
    assert isinstance(result.content[1], ImageContent)
    assert result.structured_content["experimental"] is True
    assert '"console_screenshot"' in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
