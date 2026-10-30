"""The WebView2 bridge: one page, the same service, and no way round the policy."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp import webui
from hyperv_console_mcp.capture import Screenshot
from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.webui import window
from hyperv_console_mcp.webui.api import Api


class FakeService:
    """A service with the two shapes the bridge needs, and no host behind it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def host_status(self) -> dict[str, Any]:
        self.calls.append(("host_status", {}))
        return {"hypervisor_present": True, "logical_processor_count": 12}

    def get_vm(self, vm_name: str) -> dict[str, Any]:
        self.calls.append(("get_vm", {"vm_name": vm_name}))
        return {"vm": {"name": vm_name, "state": "Running"}}

    def capture_screenshot(
        self, vm_name: str, source: str = "thumbnail", region: str = "window", max_width: int = 1600
    ) -> Screenshot:
        self.calls.append(("capture_screenshot", {"region": region, "max_width": max_width}))
        return Screenshot(b"fake-png-bytes", {"vm": vm_name, "region": region})

    def reload_policy(self) -> dict[str, Any]:
        self.calls.append(("reload_policy", {}))
        return {"levels": {}, "allowlist": []}

    def list_vms(self) -> dict[str, Any]:
        self.calls.append(("list_vms", {}))
        return {"vms": [{"name": "Lab-VM", "state": "Running"}], "allowlist_active": True}

    def events_wait(self, after: int = 0, timeout_seconds: float = 15.0) -> dict[str, Any]:
        self.calls.append(("events_wait", {"after": after, "timeout_seconds": timeout_seconds}))
        return {"events": [], "latest_sequence": after}

    def logs_subscribe(
        self,
        after: int = 0,
        *,
        vm: str | None = None,
        source: str | None = None,
        level: str | None = None,
        correlation_id: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        self.calls.append(("logs_subscribe", {
            "after": after, "vm": vm, "source": source, "level": level,
            "timeout_seconds": timeout_seconds,
        }))
        return {"records": [], "latest_sequence": after}

    def _secret(self) -> str:
        raise AssertionError("a private method must never be reachable from the page")


def _api(tmp_path: Path, level: str | None = "lab") -> Api:
    settings = Settings(
        allowed_vms=frozenset({"Lab-VM"}),
        levels={"lab-vm": level} if level else {},
        access_path=tmp_path / "access.json",
    )
    return Api(FakeService(), settings, "Lab-VM")


def test_the_page_calls_the_service_through_the_same_envelope_the_pipe_uses(tmp_path: Path) -> None:
    api = _api(tmp_path)
    envelope = api.call("get_vm", {"vm_name": "Lab-VM"})
    assert envelope["ok"]["vm"]["state"] == "Running"
    assert api.service.calls[-1] == ("get_vm", {"vm_name": "Lab-VM"})


def test_a_private_method_is_refused_rather_than_reached(tmp_path: Path) -> None:
    """`_call` and `backend` would be a second door past the policy checks."""
    api = _api(tmp_path)
    for name in ("_secret", "backend", "__init__", "not_a_method"):
        envelope = api.call(name)
        assert envelope["error"]["code"] == "invalid_argument", name


def test_a_wrong_argument_shape_is_a_refusal_with_the_reason(tmp_path: Path) -> None:
    api = _api(tmp_path)
    envelope = api.call("get_vm", {"vm": "Lab-VM"})
    assert envelope["error"]["code"] == "invalid_argument"
    assert "vm_name" in envelope["error"]["message"]


def test_hello_says_what_the_window_may_do(tmp_path: Path) -> None:
    hello = _api(tmp_path).hello()
    assert hello["vm"] == "Lab-VM"
    assert hello["level"] == "lab"
    assert hello["levels"] == ["read-only", "control", "lab"]
    assert hello["capabilities"]["guest"] is True
    assert hello["approvals"] == "listening"


def test_the_page_waits_for_events_instead_of_polling_them(tmp_path: Path) -> None:
    """One held call per burst of news, through the same envelope as any tool."""
    api = _api(tmp_path)
    envelope = api.events(7, 5)
    assert envelope["ok"] == {"events": [], "latest_sequence": 7}
    assert api.service.calls[-1] == ("events_wait", {"after": 7, "timeout_seconds": 5})


def test_hello_badges_every_vm_with_its_own_level(tmp_path: Path) -> None:
    """A level belongs to the VM, not to the workspace that happens to be open."""
    assert _api(tmp_path).hello()["levels_by_vm"] == {"lab-vm": "lab"}


def test_a_vm_with_no_level_reports_no_capability(tmp_path: Path) -> None:
    hello = _api(tmp_path, level=None).hello()
    assert hello["level"] is None
    assert not any(hello["capabilities"].values())


def test_choosing_another_vm_changes_what_the_window_asks_about(tmp_path: Path) -> None:
    api = _api(tmp_path)
    hello = api.choose("Other-VM")
    assert api.vm_name == "Other-VM"
    assert hello["vm"] == "Other-VM"

    # Closing the last workspace leaves no VM open: the page then shows the
    # fleet instead of keeping a VM the operator just closed.
    cleared = api.choose("  ")
    assert api.vm_name == ""
    assert cleared["vm"] == ""
    assert not any(cleared["capabilities"].values())


def test_setting_a_level_writes_the_operator_s_file_and_reloads(tmp_path: Path) -> None:
    """The level is the operator's, and the running service must hear it."""
    api = _api(tmp_path, level=None)
    result = api.set_level("control")
    stored = json.loads((tmp_path / "access.json").read_text(encoding="utf-8"))
    assert stored["vms"]["lab-vm"] == {"name": "Lab-VM", "level": "control"}
    assert result["level"] == "control"
    assert api.service.calls[-1][0] == "reload_policy"

    removed = api.remove_level()
    assert removed["level"] is None
    assert json.loads((tmp_path / "access.json").read_text(encoding="utf-8"))["vms"] == {}


def test_a_level_written_here_shows_up_in_this_window(tmp_path: Path) -> None:
    """The service reloads its own settings; the bridge must not keep the old ones.

    Without this the operator set a level and the window went on saying the VM
    had none — and the first-run wizard, which asks the same question, would
    have opened again on the next launch.
    """
    api = _api(tmp_path, level=None)
    assert api.hello()["level"] is None

    api.set_level("control")

    assert api.hello()["level"] == "control"
    assert api.hello()["levels_by_vm"] == {"lab-vm": "control"}
    assert api.hello()["capabilities"]["power"] is True

def test_a_level_the_file_does_not_know_is_refused(tmp_path: Path) -> None:
    api = _api(tmp_path)
    with pytest.raises(Exception):
        api.set_level("god-mode")


def test_a_request_waits_as_a_card_and_is_answered_once(tmp_path: Path) -> None:
    api = _api(tmp_path)
    request = {"request_id": "r1", "operation": "guest_exec", "vm_name": "Lab-VM",
               "summary": {"command_preview": "hostname"}}
    answers: list[bool] = []
    thread = threading.Thread(target=lambda: answers.append(api.request_approval(request)))
    thread.start()

    for _ in range(200):
        if api.approvals():
            break
        threading.Event().wait(0.01)
    cards = api.approvals()
    assert [card["request_id"] for card in cards] == ["r1"]
    assert cards[0]["summary"]["command_preview"] == "hostname"

    assert api.decide("r1", True) == {"ok": True}
    thread.join(timeout=5)
    assert answers == [True]
    assert api.approvals() == []


def test_denying_is_an_answer_too(tmp_path: Path) -> None:
    api = _api(tmp_path)
    answers: list[bool] = []
    thread = threading.Thread(target=lambda: answers.append(
        api.request_approval({"request_id": "r2", "operation": "power"})))
    thread.start()
    for _ in range(200):
        if api.approvals():
            break
        threading.Event().wait(0.01)
    api.decide("r2", False)
    thread.join(timeout=5)
    assert answers == [False]


def test_a_second_click_on_the_same_card_answers_nothing(tmp_path: Path) -> None:
    api = _api(tmp_path)
    assert api.decide("never-seen", True)["ok"] is False


def test_a_request_nobody_answers_is_denied_by_its_own_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("hyperv_console_mcp.webui.api.APPROVAL_TIMEOUT_SECONDS", 0.05)
    api = _api(tmp_path)
    assert api.request_approval({"request_id": "slow", "operation": "power"}) is False
    assert api.approvals() == []


def test_a_screenshot_becomes_a_data_url(tmp_path: Path) -> None:
    api = _api(tmp_path)
    response = api.screenshot({"region": "window"})
    assert response["ok"]["src"].startswith("data:image/png;base64,")
    assert api.service.calls[-1] == ("capture_screenshot", {"region": "window", "max_width": 1600})


# -- Hyper-V's own windows ---------------------------------------------------
# The console page is a picture of the display, so the real viewer is a button.
# It is the window that opens it, but which VM may be opened is decided here,
# against the service's own list — the allowlist included.


def _viewer(tmp_path: Path, *, installed: tuple[str, ...] = ("vmconnect.exe", "mmc.exe", "virtmgmt.msc")):
    """An Api whose SystemRoot is a temporary directory with the named programs."""
    system32 = tmp_path / "System32"
    system32.mkdir(exist_ok=True)
    for name in installed:
        (system32 / name).write_bytes(b"")
    return Api(FakeService(), Settings(allowed_vms=frozenset({"Lab-VM"})), "Lab-VM",
               environ={"SystemRoot": str(tmp_path)})


class FakeProcess:
    def __init__(self, command: list[str]) -> None:
        self.command = command
        self.pid = 4242


def test_vmconnect_opens_the_viewer_on_the_vm_the_service_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[list[str]] = []

    def popen(command, **kwargs):
        started.append(list(command))
        return FakeProcess(list(command))

    monkeypatch.setattr("hyperv_console_mcp.webui.api.subprocess.Popen", popen)
    api = _viewer(tmp_path)
    response = api.open_vmconnect("Lab-VM")
    assert response["ok"]["program"] == "vmconnect.exe"
    # The name is one argument: a VM called "Lab VM" stays one argument too.
    assert started == [[str(tmp_path / "System32" / "vmconnect.exe"), "localhost", "Lab-VM"]]


def test_a_name_with_a_space_is_still_one_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[list[str]] = []

    def popen(command, **kwargs):
        started.append(list(command))
        return FakeProcess(list(command))

    monkeypatch.setattr("hyperv_console_mcp.webui.api.subprocess.Popen", popen)
    (tmp_path / "System32").mkdir()
    (tmp_path / "System32" / "vmconnect.exe").write_bytes(b"")
    service = FakeService()
    service.list_vms = lambda: {"vms": [{"name": "Lab VM"}]}
    api = Api(service, Settings(), "Lab VM", environ={"SystemRoot": str(tmp_path)})
    assert api.open_vmconnect("Lab VM")["ok"]["arguments"] == ["localhost", "Lab VM"]
    assert started[0][-1] == "Lab VM"
    assert len(started[0]) == 3


def test_a_vm_the_host_does_not_report_is_not_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service's list is the gate: nothing starts for a name it did not give."""
    started: list[list[str]] = []
    monkeypatch.setattr(
        "hyperv_console_mcp.webui.api.subprocess.Popen",
        lambda command, **kwargs: started.append(list(command)),
    )
    api = _viewer(tmp_path)
    response = api.open_vmconnect("Some-Other-VM")
    assert response["error"]["code"] == "not_authorized"
    assert "Some-Other-VM" in response["error"]["message"]
    assert started == []


def test_vmconnect_without_a_vm_asks_for_one(tmp_path: Path) -> None:
    api = Api(FakeService(), Settings(), "", environ={"SystemRoot": str(tmp_path)})
    assert api.open_vmconnect()["error"]["code"] == "invalid_argument"


def test_a_viewer_that_is_not_installed_is_an_answer_not_a_traceback(tmp_path: Path) -> None:
    """A host without the management tools still has a window that can say so."""
    api = _viewer(tmp_path, installed=())
    response = api.open_vmconnect("Lab-VM")
    assert response["error"]["code"] == "unavailable"
    assert "vmconnect.exe" in response["error"]["message"]


def test_hyperv_manager_is_the_manager_and_not_a_reimplementation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[list[str]] = []
    monkeypatch.setattr(
        "hyperv_console_mcp.webui.api.subprocess.Popen",
        lambda command, **kwargs: started.append(list(command)) or FakeProcess(list(command)),
    )
    api = _viewer(tmp_path)
    assert api.open_hyperv_manager()["ok"]["program"] == "mmc.exe"
    assert started == [[
        str(tmp_path / "System32" / "mmc.exe"),
        str(tmp_path / "System32" / "virtmgmt.msc"),
    ]]


def test_the_page_asks_for_no_network_and_loads_nothing_remote() -> None:
    """The window is a file, not a site: no port, no CDN, no fetch."""
    html = window.page()
    assert "http://" not in html
    assert "https://" not in html
    assert "<script src" not in html
    assert "<link" not in html
    assert not re.search(r"\bimport\s*\(", html)


def test_the_page_carries_every_page_the_window_offers() -> None:
    html = window.page()
    for key in ("overview", "activity", "console", "recovery", "media", "debugger", "guest", "lab"):
        assert f"async {key}()" in html, key


def test_the_page_declares_no_name_pywebview_declares_itself() -> None:
    """A global `const value` breaks every call from the page into Python.

    pywebview answers a JS -> Python call by evaluating ``var value; ...`` at
    global scope. A page that already declares ``value`` at top level turns that
    script into a SyntaxError, so the call never returns and the window renders
    with nothing behind it: no error, no traceback, nothing to go on.
    """
    html = window.page()
    declared = re.findall(r"^(?:const|let|var|class|function)\s+([A-Za-z_$][\w$]*)", html, re.MULTILINE)
    assert "value" not in declared, "rename it; pywebview's evaluate_js wrapper uses `value`"


def test_the_window_is_unavailable_without_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(window, "webview2_installed", lambda: False)
    assert window.available() is False
    # And it says so rather than raising, so the caller can open tkinter.
    assert window.run("Lab-VM") is False


def test_a_host_without_pywebview_falls_back_instead_of_failing() -> None:
    assert isinstance(webui.window.available(), bool)


def test_the_runtime_probe_answers_a_bool_everywhere() -> None:
    assert isinstance(window.webview2_installed(), bool)

def test_the_dock_asks_the_service_for_the_lines_after_the_last_one(tmp_path: Path) -> None:
    """An empty filter is no filter, and the wait is the service's own."""
    api = _api(tmp_path)

    api.logs(41, "", "kd", "", 7)

    assert api.service.calls[-1] == ("logs_subscribe", {
        "after": 41, "vm": None, "source": "kd", "level": None, "timeout_seconds": 7,
    })

