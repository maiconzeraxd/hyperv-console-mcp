"""The WebView2 window: one HTML file, the same service, no network listener.

The page is loaded with ``html=`` and every asset is inline, so the window never
starts pywebview's local HTTP server and never opens a port — the same rule the
MCP server keeps. When pywebview or the WebView2 runtime is missing, ``run``
returns ``False`` and the caller opens the tkinter window instead of failing: a
UI dependency must not be the reason the host cannot be administered.
"""

from __future__ import annotations

import inspect
import os
from typing import Any, Mapping

from ..config import Settings
from ..errors import HyperVError, PolicyError
from .api import Api

# The Evergreen WebView2 runtime's own product id; present under one of these
# keys on every host that can render the window.
WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_KEYS = (
    (r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients", "HKLM"),
    (r"SOFTWARE\Microsoft\EdgeUpdate\Clients", "HKLM"),
    (r"SOFTWARE\Microsoft\EdgeUpdate\Clients", "HKCU"),
)
TITLE = "Hyper-V Control Center"


def webview2_installed() -> bool:
    """Whether the Evergreen WebView2 runtime is registered on this host."""
    if os.name != "nt":
        return True
    import winreg

    for path, hive_name in WEBVIEW2_KEYS:
        hive = winreg.HKEY_LOCAL_MACHINE if hive_name == "HKLM" else winreg.HKEY_CURRENT_USER
        try:
            with winreg.OpenKey(hive, rf"{path}\{WEBVIEW2_CLIENT}") as key:
                version, _kind = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        if version:
            return True
    return False


def available() -> bool:
    """pywebview importable and an engine to render it with."""
    try:
        import webview  # noqa: F401
    except Exception:  # noqa: BLE001 — a missing GUI dependency is a fallback, not an error
        return False
    return webview2_installed()


def page() -> str:
    """The window's only file, inline CSS and JS included."""
    from .page import page_text

    return page_text()


def _start_options(webview: Any) -> dict[str, Any]:
    """Only the options this pywebview version actually takes."""
    supported = set(inspect.signature(webview.start).parameters)
    options: dict[str, Any] = {"debug": False}
    # No local HTTP server: the page is passed as HTML and every asset is inline.
    if "http_server" in supported:
        options["http_server"] = False
    if os.name == "nt" and "gui" in supported:
        options["gui"] = "edgechromium"
    return options


def run(vm_name: str | None = None, *, environ: Mapping[str, str] | None = None) -> bool:
    """Open the window and return ``True`` once it closes, or ``False`` to fall back."""
    if not available():
        return False
    import webview

    from ..ipc import ApprovalBroker
    from ..service.host import open_service

    settings = Settings.from_env()
    service, backend = open_service(settings, actor="ui")
    try:
        api = Api(service, settings, _choose(service, settings, vm_name), environ=environ)
        _start_broker(api, ApprovalBroker)
        window = webview.create_window(
            TITLE, html=page(), js_api=api, width=1420, height=900,
            min_size=(1080, 720), background_color="#0b1120",
        )
        window.events.closing += lambda: _close(api, backend)
        webview.start(**_start_options(webview))
    finally:
        _close(api, backend)
    return True


def _choose(service: Any, settings: Any, requested: str | None) -> str:
    """The VM to open on: asked for, discovered, or left for the page to pick."""
    from ..dashboard import initial_vm_name

    if requested:
        return requested
    try:
        names = [str(vm["name"]) for vm in service.list_vms().get("vms") or []]
    except HyperVError:
        return ""
    return initial_vm_name(None, settings.allowed_vms, names) or ""


def _start_broker(api: Api, broker_class: Any) -> None:
    """Listen for approval requests; a window that cannot listen still opens.

    Only one process may own the approval pipe. A second window reports why in
    its header instead of refusing to start — and a request nobody can answer
    is denied by its own timeout, which is the safe direction.
    """
    broker = broker_class(api.request_approval)
    try:
        broker.start()
    except (PolicyError, OSError) as exc:
        api.approval_error = str(exc)
        return
    api.broker = broker


def _close(api: Api, backend: Any) -> None:
    broker = getattr(api, "broker", None)
    if broker is not None:
        try:
            broker.close()
        except OSError:
            pass
        api.broker = None
    if backend is not None:
        backend.close()


__all__ = ["TITLE", "available", "page", "run", "webview2_installed"]
