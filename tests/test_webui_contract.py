"""The page is a client, and this is the contract it is held to.

Every action in the window is a call to the service — the same object the MCP
server holds — or to one of the few things the window owns (the access file, the
approval queue). Nothing is decided in JavaScript, so a method the page names
that the service does not have is a broken button, and a name that only exists
in the page would be a second implementation of a rule.

The second half is parity: an operation the operator can perform by clicking is
an operation the AI can ask for, or it is an asymmetry the product does not
mean to have. The MCP surface is read out of ``server.py`` by name, so a tool
that stops reaching a method is caught here rather than by a user.

The file is read as text rather than executed: there is no DOM here, and the
property being checked is about names, not about rendering.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from hyperv_console_mcp.server import ConsoleAction
from hyperv_console_mcp.service import HyperVService
from hyperv_console_mcp.webui.api import Api

ROOT = Path(__file__).resolve().parents[1] / "src" / "hyperv_console_mcp"
PAGE = ROOT / "webui" / "assets" / "index.html"
SERVER = ROOT / "server.py"

# Operations the page sends one at a time and the AI sends as a batch:
# `console_input` carries the same five actions through `console_batch`, which is
# one bridge request and one approval. The second half of each pair is the action
# type the batch accepts, so the equivalence is checked rather than asserted.
BATCHED = {
    "console_click": ("console_batch", "click"),
    "console_mouse_move": ("console_batch", "move"),
    "console_press_keys": ("console_batch", "keys"),
    "console_type_text": ("console_batch", "text"),
    "console_ctrl_alt_delete": ("console_batch", "ctrl_alt_delete"),
}


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def service_calls() -> set[str]:
    """The method names the page passes to the service envelope."""
    text = _page()
    # `call("x", …)` and `api().call("x", …)` are the same door.
    names = set(re.findall(r'call\(\s*"([A-Za-z_][A-Za-z0-9_]*)"', text))
    # `action("Label", "x", …)` builds a button that calls `x`.
    names |= set(re.findall(r'action\(\s*"[^"]*",\s*"([A-Za-z_][A-Za-z0-9_]*)"', text))
    return names


def bridge_calls() -> set[str]:
    """The methods the page calls on the pywebview bridge itself."""
    return set(re.findall(r"api\(\)\.([A-Za-z_][A-Za-z0-9_]*)", _page()))


def test_the_page_names_at_least_the_surface_it_should() -> None:
    """A parser that silently matched nothing would make the checks below empty."""
    assert {"list_vms", "get_vm", "power", "console_click"} <= service_calls()
    assert {"hello", "choose", "screenshot", "events"} <= bridge_calls()


def test_every_service_method_the_page_names_exists() -> None:
    public = {name for name in dir(HyperVService) if not name.startswith("_")}
    assert sorted(service_calls() - public) == []


def test_every_bridge_method_the_page_names_exists() -> None:
    public = {name for name in dir(Api) if not name.startswith("_")}
    assert sorted(bridge_calls() - public) == []


def test_the_page_never_reaches_for_a_private_name() -> None:
    assert [name for name in service_calls() | bridge_calls() if name.startswith("_")] == []


def test_the_page_names_no_vm_of_this_household() -> None:
    """The window ships to a host it has never seen; a default name is a guess."""
    text = _page()
    assert "NORTH-Test" not in text
    assert "north-kd" not in text


# -- parity: what the operator clicks, the AI can ask for --------------------


def mcp_service_methods() -> set[str]:
    """Every service method an MCP tool reaches, read from ``server.py``.

    The tools call ``service.<method>`` directly, so the attribute names in the
    module are the reachable set. A tool that went through a dictionary of names
    would be invisible here, which is why none does.
    """
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "service"
    }


def test_the_mcp_surface_is_actually_read() -> None:
    """A parser that matched nothing would make the parity check below empty."""
    reachable = mcp_service_methods()
    assert {"list_vms", "get_vm", "power", "lab_cycle", "kd_inspect"} - {"kd_inspect"} <= reachable
    assert len(reachable) > 50


def test_every_operation_the_window_offers_is_one_the_ai_can_ask_for() -> None:
    reachable = mcp_service_methods()
    missing = sorted(
        name for name in service_calls()
        if name not in reachable and BATCHED.get(name, ("",))[0] not in reachable
    )
    assert missing == []


def test_the_batched_operations_are_the_actions_the_batch_takes() -> None:
    """The equivalence is real, not a note in a comment."""
    # The discriminator's literals are the action names `console_batch` accepts.
    actions = {
        member.model_fields["type"].annotation.__args__[0]
        for member in ConsoleAction.__origin__.__args__
    }
    for method, (composite, action) in BATCHED.items():
        assert composite in mcp_service_methods(), method
        assert action in actions, method
        assert hasattr(HyperVService, method), method
