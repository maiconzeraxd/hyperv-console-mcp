"""The bare command is the window, and the server only when a client drives it.

``hyperv-console-mcp`` is two things at once: the console script an MCP client
points at, and the thing a person double-clicks. Which one it is has to be
decided without asking, so it is decided by stdin — a client always hands the
process a pipe, a person has a console, and a windowed executable has no stdin
at all. The guess is a guess, which is why ``--stdio`` and ``--ui`` exist.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyperv_console_mcp import __version__, cli


def _stdin(isatty: Any) -> Any:
    """A stand-in for stdin: whether it is a console, or what ``isatty`` raises."""
    if isinstance(isatty, BaseException):

        def raising() -> bool:
            raise isatty

        return SimpleNamespace(isatty=raising)
    return SimpleNamespace(isatty=lambda: isatty)


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record which door ``main`` chose, without opening either."""
    calls: list[Any] = []
    monkeypatch.setattr(cli, "_serve", lambda: calls.append("serve") or 0)
    monkeypatch.setattr(
        cli, "_window", lambda arguments: calls.append(("window", list(arguments))) or 0
    )
    return calls


def test_a_console_is_a_person_asking_for_the_window() -> None:
    assert cli.wants_window(_stdin(True)) is True


def test_a_pipe_is_a_client_asking_for_the_server() -> None:
    assert cli.wants_window(_stdin(False)) is False


def test_no_stdin_at_all_is_a_double_click() -> None:
    """A windowed executable has no stdin; nothing typed a request into it."""
    assert cli.wants_window(None) is True


@pytest.mark.parametrize("failure", [AttributeError("no isatty"), ValueError("closed"), OSError("gone")])
def test_a_handle_that_cannot_answer_is_a_client(failure: BaseException) -> None:
    """A client that closed its end still asked for the server."""
    assert cli.wants_window(_stdin(failure)) is False


def test_the_bare_command_serves_the_client_that_gave_it_a_pipe(
    ran: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.sys, "stdin", _stdin(False))
    assert cli.main([]) == 0
    assert ran == ["serve"]


def test_the_bare_command_opens_the_window_for_a_person(
    ran: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.sys, "stdin", _stdin(True))
    assert cli.main([]) == 0
    assert ran == [("window", [])]


def test_the_bare_command_opens_the_window_with_no_stdin(
    ran: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.sys, "stdin", None)
    assert cli.main([]) == 0
    assert ran == [("window", [])]


def test_the_command_reads_its_arguments_from_the_process_when_given_none(
    ran: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the console script and the shortcut both actually do."""
    monkeypatch.setattr(cli.sys, "argv", ["hyperv-console-mcp", "--ui"])
    assert cli.main() == 0
    assert ran == [("window", [])]


@pytest.mark.parametrize("stdin", [True, False, None])
def test_stdio_is_the_server_whatever_stdin_looks_like(
    ran: list[Any], monkeypatch: pytest.MonkeyPatch, stdin: Any
) -> None:
    """The explicit form, for a script that must not depend on the guess."""
    monkeypatch.setattr(cli.sys, "stdin", _stdin(stdin) if stdin is not None else None)
    assert cli.main(["--stdio"]) == 0
    assert ran == ["serve"]


@pytest.mark.parametrize(
    ("arguments", "forwarded"),
    [
        (["--ui"], []),
        (["--dashboard"], []),
        (["--classic"], ["--classic"]),
        (["--vm", "lab-vm"], ["--vm", "lab-vm"]),
        (["--vm=lab-vm"], ["--vm=lab-vm"]),
    ],
)
def test_the_window_flags_open_the_window(
    ran: list[Any], monkeypatch: pytest.MonkeyPatch, arguments: list[str], forwarded: list[str]
) -> None:
    """The launcher's own words are dropped; the dashboard's are handed on unchanged."""
    monkeypatch.setattr(cli.sys, "stdin", _stdin(False))
    assert cli.main(arguments) == 0
    assert ran == [("window", forwarded)]


def test_an_argument_the_launcher_does_not_know_is_left_to_the_server(
    ran: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that sends one is asking for the server and should hear from it."""
    monkeypatch.setattr(cli.sys, "stdin", _stdin(True))
    assert cli.main(["--something-new"]) == 0
    assert ran == ["serve"]


def test_version_is_answered_without_opening_anything(
    ran: list[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__
    assert ran == []


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_lists_the_two_doors(
    ran: list[Any], capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    assert cli.main([flag]) == 0
    printed = capsys.readouterr().out
    assert "--stdio" in printed
    assert "--ui" in printed
    assert ran == []


def test_the_console_script_is_the_launcher_not_the_server() -> None:
    """The shortcut runs this bare command; it must not drift back to the server.

    A client's config names the same script, so the two callers share one entry
    point and the guess above is what tells them apart.
    """
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    assert project["scripts"]["hyperv-console-mcp"] == "hyperv_console_mcp.cli:main"
