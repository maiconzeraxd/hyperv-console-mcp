"""The page is a client, and this is the contract it is held to.

Every action in the window is a call to the service — the same object the MCP
server holds — or to one of the few things the window owns (the access file, the
approval queue). Nothing is decided in JavaScript, so a method the page names
that the service does not have is a broken button, and a name that only exists
in the page would be a second implementation of a rule.

The file is read as text rather than executed: there is no DOM here, and the
property being checked is about names, not about rendering.
"""

from __future__ import annotations

import re
from pathlib import Path

from hyperv_console_mcp.service import HyperVService
from hyperv_console_mcp.webui.api import Api

PAGE = (
    Path(__file__).resolve().parents[1]
    / "src" / "hyperv_console_mcp" / "webui" / "assets" / "index.html"
)


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
