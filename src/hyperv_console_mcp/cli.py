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
    from .dashboard import main as dashboard_main

    dashboard_main(arguments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
