"""Every refusal that can name a next step names one, in a shape a caller can use.

A fix is either a tool call (``{"tool", "args"}``) or a note for the operator
(``{"operator"}``). The first kind is only worth emitting if the tool exists and
accepts those arguments, so that is checked against the registered surface and
the tool's own schema rather than against a list someone must remember to
update.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_service import FakeBackend

from hyperv_console_mcp import credentials
from hyperv_console_mcp.access import minimum_level
from hyperv_console_mcp.config import TOOLSETS, Settings
from hyperv_console_mcp.errors import PolicyError, ValidationError
from hyperv_console_mcp.server import _failure, create_server
from hyperv_console_mcp.service import HyperVService

SOURCE = Path(__file__).resolve().parents[1] / "src" / "hyperv_console_mcp"
FIX = re.compile(r'"tool":\s*"([a-z0-9_]+)"\s*,\s*"args":\s*\{([^{}]*)\}')
KEY = re.compile(r'"([a-z0-9_]+)":')
LITERAL = re.compile(r'"([a-z0-9_]+)":\s*"([^"]*)"')
VM = "LAB-Test"


def registered() -> dict[str, Any]:
    """Every tool the widest profile exposes, by name."""
    service = HyperVService(FakeBackend(), Settings(toolsets=TOOLSETS))
    return {tool.name: tool for tool in anyio.run(create_server(service).list_tools)}


def fixes() -> list[tuple[str, str, str]]:
    """``(module, tool, args-as-written)`` for every tool fix in the package."""
    found: list[tuple[str, str, str]] = []
    for path in sorted(SOURCE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for tool, args in FIX.findall(text):
            found.append((path.name, tool, args))
    return found


def test_a_refusal_without_a_fix_stays_one_line() -> None:
    """The common case pays nothing for the contract."""
    assert _failure(ValidationError("vm_name must be a non-empty string.")) == (
        "invalid_argument: vm_name must be a non-empty string."
    )


def test_a_fix_is_rendered_as_json_a_caller_can_parse() -> None:
    """Prose a model has to read is not a next step; JSON is."""
    error = PolicyError(
        "the VM is running",
        fix={"tool": "vm_power", "args": {"vm_name": VM, "action": "shutdown"}},
    )
    head, _, payload = _failure(error).partition("\nfix: ")
    assert head == "not_authorized: the VM is running"
    assert json.loads(payload) == {
        "args": {"action": "shutdown", "vm_name": VM},
        "tool": "vm_power",
    }


def test_a_tool_failure_carries_the_fix_to_the_client() -> None:
    """The rendering above is only worth anything if the server uses it."""
    from hyperv_console_mcp import server as server_module

    error = PolicyError("nope", fix={"operator": "run hyperv-access"})

    def fail() -> None:
        raise error

    with pytest.raises(ToolError) as caught:
        anyio.run(server_module._invoke, fail)
    assert "fix:" in str(caught.value)
    assert "hyperv-access" in str(caught.value)


def test_every_fix_that_names_a_tool_names_a_registered_one() -> None:
    """A fix pointing at a tool nobody registered is worse than no fix."""
    tools = registered()
    found = fixes()
    # Two producers exist today; a pattern that silently stopped matching would
    # otherwise turn this whole file into a no-op.
    assert len(found) >= 2
    unknown = sorted({tool for _module, tool, _args in found if tool not in tools})
    assert unknown == []


def test_every_fix_passes_arguments_the_tool_accepts() -> None:
    """The schema is the contract: a required argument missing, or an unknown
    one, or a literal outside an enum, makes the suggested call fail."""
    tools = registered()
    problems: list[str] = []
    for module, tool, args in fixes():
        schema = tools[tool].input_schema
        properties = schema.get("properties") or {}
        keys = set(KEY.findall(args))
        missing = sorted(set(schema.get("required") or []) - keys)
        if missing:
            problems.append(f"{module}: {tool} fix omits {missing}")
        unknown = sorted(keys - set(properties))
        if unknown:
            problems.append(f"{module}: {tool} fix passes unknown {unknown}")
        for key, value in LITERAL.findall(args):
            allowed = (properties.get(key) or {}).get("enum")
            if allowed and value not in allowed:
                problems.append(f"{module}: {tool} fix sets {key}={value!r} outside its enum")
    assert problems == []


def test_a_missing_credential_asks_the_operator_and_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(credentials, "_read", lambda target: None)
    settings = Settings(
        allowed_vms=frozenset({VM}),
        guest_username="",
        guest_password="s3cret-value-that-must-not-surface",
    )
    with pytest.raises(PolicyError) as caught:
        credentials.resolve(settings, VM)
    fix = caught.value.fix or {}
    assert set(fix) == {"operator"}
    assert VM in fix["operator"]
    assert "s3cret-value-that-must-not-surface" not in str(caught.value)
    assert "s3cret-value-that-must-not-surface" not in json.dumps(fix)


def test_a_level_refusal_names_the_smallest_level_that_would_work() -> None:
    service = HyperVService(
        FakeBackend(),
        Settings(allowed_vms=frozenset({VM}), levels={VM.casefold(): "control"}),
    )
    with pytest.raises(PolicyError) as caught:
        service.guest_exec(VM, "hostname")
    assert caught.value.fix == {"operator": f'hyperv-access set "{VM}" lab'}


def test_a_vm_with_no_level_is_offered_the_level_and_the_old_flag() -> None:
    """Both answers exist for an unlevelled VM; the level is the narrow one."""
    service = HyperVService(
        FakeBackend(),
        Settings(allowed_vms=frozenset({VM}), allow_guest=False),
    )
    with pytest.raises(PolicyError) as caught:
        service.guest_exec(VM, "hostname")
    operator = (caught.value.fix or {})["operator"]
    assert f'hyperv-access set "{VM}" lab' in operator
    assert "HYPERV_MCP_ALLOW_GUEST=true" in operator


def test_a_vm_outside_the_allowlist_says_how_to_add_it() -> None:
    service = HyperVService(FakeBackend(), Settings(allowed_vms=frozenset({VM})))
    with pytest.raises(PolicyError) as caught:
        service.guest_exec("OTHER-VM", "hostname")
    assert caught.value.fix == {"operator": 'hyperv-access set "OTHER-VM" read-only|control|lab'}


@pytest.mark.parametrize(
    "capability, level",
    [("power", "control"), ("guest", "lab"), ("debugger", "lab"), ("media", "control")],
)
def test_the_level_a_fix_names_is_the_lowest_one_that_includes_the_capability(
    capability: str, level: str
) -> None:
    assert minimum_level(capability) == level
