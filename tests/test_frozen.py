"""A frozen build has no interpreter to name, so it names its siblings.

Three places choose a program: the client entry an install writes, the shortcut
it creates, and the host a client starts. Under an interpreter all three are
``python -m …``; in a PyInstaller folder they are files beside the executable.
Getting one of them wrong is not a cosmetic bug — a client entry naming the
installer reinstalls the machine every time the AI starts, and a host command
naming the launcher starts a second server on a pipe nobody reads.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hyperv_console_mcp import frozen, ipc
from hyperv_console_mcp.service import host


def test_a_normal_run_is_not_frozen() -> None:
    assert frozen.is_frozen() is False


def test_a_frozen_run_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert frozen.is_frozen() is True


def test_a_sibling_is_beside_the_executable() -> None:
    assert frozen.sibling("hyperv-service.exe") == Path(sys.executable).with_name(
        "hyperv-service.exe"
    )


def test_the_host_a_frozen_client_starts_is_the_program_beside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert host._spawn_command() == [str(Path(sys.executable).with_name(frozen.HOST))]


def test_the_host_under_an_interpreter_is_the_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert host._spawn_command() == [
        sys.executable, "-m", "hyperv_console_mcp.service.host",
    ]


def test_the_host_is_started_detached_and_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It has to outlive the client that asked for it, so its output goes to a file."""
    seen: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(host, "service_log", lambda: tmp_path / "service.log")
    monkeypatch.setattr(
        host.subprocess, "Popen", lambda command, **options: seen.append((command, options))
    )

    host._spawn()

    assert seen[0][0] == host._spawn_command()
    assert seen[0][1]["stdin"] == host.subprocess.DEVNULL
    assert (tmp_path / "service.log").is_file()


def test_an_interpreter_trusts_a_python_image_only() -> None:
    # Written with forward slashes so `Path` splits it into a name on every host:
    # this is a rule about file names, and the test runs on Linux too.
    assert ipc._trusted_image(Path("C:/Python311/python.exe")) is True
    # No interpreter is frozen beside a hyperv-service.exe in a plain checkout.
    assert ipc._trusted_image(Path("C:/Tools/hyperv-service.exe")) is False


def test_a_frozen_install_trusts_its_own_siblings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service is not a Python process, and refusing it made every call fail.

    The folder is part of the check: a copy of the executable elsewhere is not
    the program the installer wrote.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    beside = Path(sys.executable).parent
    assert ipc._trusted_image(beside / frozen.HOST) is True
    assert ipc._trusted_image(beside / frozen.LAUNCHER) is True
    assert ipc._trusted_image(beside / "notepad.exe") is False
    assert ipc._trusted_image(Path(r"C:\Elsewhere") / frozen.HOST) is False
