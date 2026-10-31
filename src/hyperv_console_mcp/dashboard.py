"""The classic control center: what the window needs before tkinter exists.

Since v0.4 the interface is the page in a WebView2 window, and this window is
the fallback for a host that cannot draw one. That makes tkinter an *optional*
dependency — so it is not imported here. Importing this module works on a host
with no Tk at all (the Linux CI job, a Python built without it), which is what
lets the fallback path fail with a sentence instead of a traceback at import
time.

Everything that draws lives in :mod:`hyperv_console_mcp.dashboard_tk`, imported
only when somebody actually asks for the classic window. The pieces left here
are the ones other modules use without drawing anything: ``initial_vm_name``
(the WebView2 window picks its VM with it) and the notes the page and the
window both show.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import HyperVError

COLORS = {
    "background": "#0b1120", "panel": "#111b2e", "surface": "#17243a",
    "border": "#273851", "text": "#e5edf8", "muted": "#8fa5bf",
    "accent": "#6ee7b7", "blue": "#60a5fa", "warning": "#fbbf24", "error": "#fb7185",
}


def _safe_json(value: Any) -> str:
    def sanitize(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: sanitize(val) for key, val in item.items() if key not in {"password", "content_base64"}}
        if isinstance(item, list):
            return [sanitize(val) for val in item]
        if isinstance(item, str) and len(item) > 16_384:
            return item[:16_384] + "… [truncated]"
        return item
    return json.dumps(sanitize(value), ensure_ascii=False, indent=2)


def _format_summary(summary: Any) -> str:
    """Render an approval summary for a queue card, with the command preview first."""
    if not isinstance(summary, dict) or not summary:
        return "No additional details were provided for this operation."
    preview = summary.get("command_preview")
    if not isinstance(preview, str):
        return json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
    rest = {key: value for key, value in summary.items() if key != "command_preview"}
    return "\n".join([
        "Command preview (the guest runs exactly this):",
        "",
        preview,
        "",
        json.dumps(rest, ensure_ascii=False, indent=2, sort_keys=True),
    ])


def initial_vm_name(
    requested: str | None, allowed: frozenset[str], discovered: list[str]
) -> str | None:
    """The VM to open without asking, or ``None`` when the operator must choose.

    An explicit ``--vm`` wins. Otherwise the candidates are what Hyper-V
    reports, narrowed by the allowlist when one is configured — so a host with
    a single VM opens it with no configuration at all, while a host with two
    asks rather than guessing which one the operator meant.
    """
    if requested:
        return requested
    candidates = [
        name
        for name in discovered
        if not allowed or any(name.casefold() == entry.casefold() for entry in allowed)
    ]
    return candidates[0] if len(candidates) == 1 else None


def existing_pipe_name(status: dict[str, Any]) -> str:
    """The pipe COM1 is already on, or ``''`` when it is on none.

    A derived name is only a suggestion. A port that was configured earlier —
    by hand, by WinDbg, or by an older installation — keeps the name the host
    reports, and the debugger page should show that rather than a new guess.
    """
    ports = status.get("com_ports") or []
    first = next((port for port in ports if port.get("number") == 1), None)
    path = str((first or {}).get("path") or "")
    return path.rsplit("\\", 1)[-1] if path else ""


def access_note(vm_name: str, levels: Mapping[str, str]) -> str:
    """What the window says about this VM's level, in one sentence.

    A level is the whole policy in one word, so the sentence says what the
    absence of one means as well as what a level allows.
    """
    level = levels.get(vm_name.strip().casefold())
    if level:
        return f"{vm_name} is {level}. The service reloads the file when you change it."
    return f"{vm_name} has no level, so no tool can reach it. Choose one and apply it."



class NoTkinter(HyperVError):
    """The classic window was asked for on a Python that has no Tk."""


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vm",
        help="Virtual machine to control. Omit it to open the only VM Hyper-V reports, or to choose from a list.",
    )
    parser.add_argument(
        "--classic", action="store_true",
        help="Use the tkinter window instead of the WebView2 one.",
    )
    arguments = parser.parse_args(argv)
    try:
        if not arguments.classic:
            from .webui.window import run

            # The WebView2 window is the default; a host without the runtime keeps
            # the tkinter one rather than losing the only way to administer it.
            if run(arguments.vm):
                return
        _classic_window(arguments.vm)
    except NoTkinter as exc:
        # No Tk means no message box either: the sentence goes to the console,
        # which is the only place left.
        print(f"hyperv-console-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except HyperVError as exc:
        # Launched from the shortcut there is no console to read, so a service
        # that will not start has to say so on screen instead of vanishing.
        _report_failure(str(exc))
        raise SystemExit(1) from None


def _classic_window(vm_name: str | None) -> None:
    """Open the tkinter window, or say why this Python cannot.

    The import is the door: a host without Tk gets a named error from here, not
    a ``ModuleNotFoundError`` from loading this module.
    """
    try:
        from . import dashboard_tk
    except ImportError as exc:
        raise NoTkinter(
            "The classic window needs tkinter, and this Python does not have it. "
            "Install the system Tk package (python3-tk), or install the WebView2 "
            'window with: pip install "hyperv-console-mcp[ui]"'
        ) from exc
    dashboard_tk.open_window(vm_name)


def _report_failure(message: str) -> None:
    """Say why the window cannot open, where the operator will see it."""
    try:
        from .dashboard_tk import report_failure
    except ImportError:
        print(message, file=sys.stderr)
        return
    report_failure(message)


if __name__ == "__main__":
    main()
