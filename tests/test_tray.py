"""``hyperv-tray`` is a menu, a tooltip and nothing else.

The tray decides nothing: the window it opens is the launcher's own command,
and the line it shows is the answer the service gives the AI. What is tested
here is that it says the truth when the host is down, that it does not put a VM
name on a shared desktop, and that its three items do what they are named —
all of it with ``pystray`` faked, since the icon is an optional extra.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyperv_console_mcp import tray
from hyperv_console_mcp.errors import PolicyError


class _FakeService:
    def __init__(self, vms: list[dict[str, Any]], *, failure: Exception | None = None) -> None:
        self.vms = vms
        self.failure = failure
        self.calls: list[str] = []

    def host_status(self) -> dict[str, Any]:
        self.calls.append("host_status")
        if self.failure is not None:
            raise self.failure
        return {"profile": "lab", "hypervisor_present": True}

    def list_vms(self) -> dict[str, Any]:
        self.calls.append("list_vms")
        if self.failure is not None:
            raise self.failure
        return {"vms": self.vms}


def test_the_line_counts_the_fleet_and_names_the_profile() -> None:
    line = tray.status_text(
        {"profile": "lab"},
        {"vms": [{"name": "a", "state": "Running"}, {"name": "b", "state": "Off"},
                 {"name": "c", "state": "running"}]},
    )
    assert line == "3 VMs · 2 running · lab profile"


def test_one_vm_is_not_one_vms() -> None:
    assert tray.status_text({"profile": "safe"}, {"vms": [{"name": "a", "state": "Off"}]}) == (
        "1 VM · 0 running · safe profile"
    )


def test_a_host_that_hyper_v_is_not_on_says_so() -> None:
    """The tooltip is where a broken host is noticed before the window opens."""
    line = tray.status_text({"profile": "safe", "hypervisor_present": False}, {"vms": []})
    assert line == "0 VMs · 0 running · safe profile · Hyper-V is off"


def test_the_line_never_names_a_virtual_machine() -> None:
    """A tooltip sits on whatever desktop the tray is on."""
    line = tray.status_text(
        {"profile": "lab"}, {"vms": [{"name": "NORTH-Test", "state": "Running"}]}
    )
    assert "NORTH-Test" not in line


def test_the_line_comes_from_the_two_calls_the_window_also_makes() -> None:
    service = _FakeService([{"name": "a", "state": "Running"}])
    line, answered = tray.read_status(service)
    assert answered is True
    assert line == "1 VM · 1 running · lab profile"
    assert service.calls == ["host_status", "list_vms"]


def test_a_service_that_refuses_is_the_answer_not_an_exception() -> None:
    line, answered = tray.read_status(_FakeService([], failure=PolicyError("The pipe is gone.")))
    assert answered is False
    assert line == "Not answering: The pipe is gone."


def test_no_service_at_all_is_also_an_answer() -> None:
    assert tray.read_status(None) == ("Not answering: the service is not running.", False)


def test_the_window_is_the_command_a_person_types() -> None:
    assert tray.window_command() == [sys.executable, "-m", "hyperv_console_mcp.cli", "--ui"]


def test_a_frozen_build_opens_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert tray.window_command() == [sys.executable, "--ui"]


def test_a_second_click_does_not_open_a_second_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the window this tray started is counted; closing it frees the click."""
    spawned: list[Any] = []

    class _Process:
        code: int | None = None

        def poll(self) -> int | None:
            return self.code

    def popen(command: list[str], **options: Any) -> _Process:
        process = _Process()
        spawned.append((command, options, process))
        return process

    monkeypatch.setattr(tray, "_window", None)
    monkeypatch.setattr(tray.subprocess, "Popen", popen)

    tray.open_window()
    tray.open_window()
    assert len(spawned) == 1
    assert spawned[0][0] == tray.window_command()
    assert spawned[0][1]["close_fds"] is True

    spawned[0][2].code = 0
    tray.open_window()
    assert len(spawned) == 2


def test_the_icon_is_drawn_rather_than_shipped() -> None:
    image = tray.icon_image()
    assert image.size == (64, 64)
    assert image.mode == "RGBA"


def test_without_pystray_the_command_says_what_to_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "pystray", None)
    assert tray.main() == 2
    assert "hyperv-console-mcp[tray]" in capsys.readouterr().err


class _Item:
    def __init__(self, text: str, action: Any, **options: Any) -> None:
        self.text = text
        self.action = action
        self.options = options


class _Menu:
    def __init__(self, *items: Any) -> None:
        self.items = list(items)


class _Icon:
    registry: list[_Icon] = []

    def __init__(self, name: str, image: Any = None, title: Any = None, menu: Any = None) -> None:
        self.name = name
        self.image = image
        self.title = title
        self.menu = menu
        self.notified: list[tuple[str, str | None]] = []
        self.updated = 0
        self.stopped = False
        self.ran = False
        _Icon.registry.append(self)

    def notify(self, message: str, title: str | None = None) -> None:
        self.notified.append((message, title))

    def update_menu(self) -> None:
        self.updated += 1

    def stop(self) -> None:
        self.stopped = True

    def run(self, setup: Any = None) -> None:
        self.ran = True


@pytest.fixture
def fake_pystray(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    _Icon.registry = []
    module = SimpleNamespace(Icon=_Icon, Menu=_Menu, MenuItem=_Item)
    monkeypatch.setitem(sys.modules, "pystray", module)
    return module


def test_the_console_script_is_the_tray() -> None:
    """The extra is optional, but the command that uses it is installed by name."""
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    assert project["scripts"]["hyperv-tray"] == "hyperv_console_mcp.tray:main"
    assert any(dependency.startswith("pystray") for dependency in project["optional-dependencies"]["tray"])


def test_the_three_items_are_open_status_quit_and_do_what_they_say(
    fake_pystray: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[int] = []

    class _Private:
        closed = False

        def close(self) -> None:
            self.closed = True

    private = _Private()
    service = _FakeService([{"name": "a", "state": "Running"}])
    monkeypatch.setattr(tray, "tray_service", lambda: (service, private))
    monkeypatch.setattr(tray, "open_window", lambda: opened.append(1))

    assert tray.main() == 0
    icon = _Icon.registry[0]
    assert [item.text for item in icon.menu.items] == list(tray.MENU_LABELS)
    assert icon.ran is True
    assert icon.title == "Hyper-V Console — 1 VM · 1 running · lab profile"
    assert private.closed is True

    for item in icon.menu.items:
        item.action(icon, item)
    assert opened == [1]
    assert icon.notified[0][0] == "1 VM · 1 running · lab profile"
    assert icon.stopped is True


def test_a_host_that_will_not_answer_still_gets_an_icon(
    fake_pystray: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No icon would be the worst outcome: it is how the operator finds out."""

    def refuse() -> Any:
        raise PolicyError("The shared service did not answer within 20 seconds.")

    monkeypatch.setattr(tray, "tray_service", refuse)
    assert tray.main() == 0
    icon = _Icon.registry[0]
    assert "did not answer" in icon.title
    assert "did not answer" in capsys.readouterr().err
