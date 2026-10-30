"""``hyperv-console-mcp`` with no arguments: the window, not a server.

The command is the MCP server and stays the MCP server — a client writes
JSON-RPC into stdin, and a client's stdin is never a terminal. A person in a
terminal is, and a double-clicked executable has no stdin at all: both of those
are somebody asking for the window, which is what a fresh install's shortcut and
its one command should give them.

The guess is a guess, so it has explicit forms: ``--stdio`` for a script that
wants the server whatever stdin looks like, and ``--ui`` for the window. An
unrecognised argument is not refused here: a client that sends one is asking
for the server, and the server is what answers it.

Nothing in this module decides anything about the policy. It chooses which of
two existing entry points to run, and it adds no door of its own.
"""

from __future__ import annotations

import sys
from typing import Any, Sequence

# The first argument that means "the window". `--vm NAME` and `--vm=NAME` are
# the dashboard's own flags, so they are recognised by prefix below.
WINDOW_FLAGS = ("--ui", "--dashboard", "--classic", "--vm")

# Words for "the window" that only this launcher understands. The dashboard's
# parser would refuse them, so they are dropped before it is handed the rest.
WINDOW_ONLY = ("--ui", "--dashboard")

USAGE = """\
hyperv-console-mcp — the MCP server, and the window that belongs to it.

  hyperv-console-mcp              open the window. An MCP client always gives
                                  the process a pipe, and a pipe gets the
                                  server instead, so this is safe in a config.
  hyperv-console-mcp --stdio      always run the MCP server on stdin/stdout.
  hyperv-console-mcp --ui         always open the window.
  hyperv-console-mcp --vm NAME    open the window on one virtual machine.
  hyperv-console-mcp --classic    open the tkinter window instead of WebView2.
  hyperv-console-mcp --serve      serve the same page in your browser, on
                                  127.0.0.1 with a token (--port N, --vm NAME).
  hyperv-console-mcp --version    print the version and exit.
"""


def _say(text: str) -> None:
    """Write to stdout as UTF-8, whatever the stream was opened with.

    The help goes to a console, where the text is written through the console
    API and needs no encoding; captured to a pipe, it takes the locale's code
    page, and cp1252 turns the em dash into a byte no reader can decode. The
    server writes UTF-8 on this same stream, so the help matches it.
    """
    stream = sys.stdout
    try:
        stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        # A stream that cannot be reconfigured still gets the text.
        pass
    stream.write(text)


def wants_window(stream: Any) -> bool:
    """Whether this invocation came from a person rather than from a client.

    A client gives the server a pipe it writes requests into. A terminal is a
    terminal, and a windowed executable has no stdin at all — both of those mean
    somebody typed the command or double-clicked something.
    """
    if stream is None:
        return True
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        # A closed or redirected handle is not a console: a client that closed
        # its end still asked for the server.
        return False


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        return _window([]) if wants_window(sys.stdin) else _serve()
    first = arguments[0]
    if first == "--stdio":
        return _serve()
    if first == "--serve":
        return _browser(arguments[1:])
    if first in WINDOW_FLAGS or first.startswith("--vm="):
        return _window([item for item in arguments if item not in WINDOW_ONLY])
    if first in ("-h", "--help"):
        _say(USAGE)
        return 0
    if first == "--version":
        from . import __version__

        _say(f"{__version__}\n")
        return 0
    return _serve()


def _serve() -> int:
    from .server import main as server_main

    server_main()
    return 0


def _window(arguments: list[str]) -> int:
    """The interface: the page in a WebView2 window, or the dashboard without it.

    Since v0.4 the page is the interface, and this is what ``hyperv-console-mcp``
    with no arguments runs. Opening it also brings the shared service up: the
    window connects through ``open_service``, which starts the host when nothing
    is listening — one command, nothing to configure, and a second launch joins
    the service instead of starting another.

    The tkinter dashboard stays as the fallback for a host without the WebView2
    runtime, because the alternative is a command that opens nothing at all.
    """
    from .webui import window as interface

    if interface.available() and interface.run(_vm_flag(arguments)):
        return 0
    from .dashboard import main as dashboard_main

    dashboard_main(arguments)
    return 0


def _vm_flag(arguments: Sequence[str]) -> str | None:
    """``--vm NAME`` or ``--vm=NAME``, which is all the window takes."""
    rest = list(arguments)
    while rest:
        item = rest.pop(0)
        if item == "--vm" and rest:
            return rest.pop(0)
        if item.startswith("--vm="):
            return item.split("=", 1)[1]
    return None


def _browser(arguments: list[str]) -> int:
    """``--serve``: the same page in the operator's browser, loopback and token.

    The options are parsed here rather than by the page's own module so a typo
    is answered in the terminal instead of opening a listener that ignores it.
    """
    port = 0
    vm_name = ""
    rest = list(arguments)
    while rest:
        item = rest.pop(0)
        try:
            if item == "--port" and rest:
                port = int(rest.pop(0))
            elif item.startswith("--port="):
                port = int(item.split("=", 1)[1])
            elif item == "--vm" and rest:
                vm_name = rest.pop(0)
            elif item.startswith("--vm="):
                vm_name = item.split("=", 1)[1]
            else:
                _say(f"hyperv-console-mcp: {item} is not a --serve option.\n")
                return 2
        except ValueError:
            _say(f"hyperv-console-mcp: {item} needs a number.\n")
            return 2
    from .webui.serve import run

    return run(port=port, open_browser=True, vm_name=vm_name)


if __name__ == "__main__":
    raise SystemExit(main())
