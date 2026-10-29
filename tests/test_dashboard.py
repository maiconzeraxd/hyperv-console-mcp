from __future__ import annotations

import os
import queue
import time
from pathlib import Path

import pytest

pytest.importorskip("tkinter", reason="the dashboard is a tkinter application")

from hyperv_console_mcp.dashboard import Dashboard, existing_pipe_name, initial_vm_name  # noqa: E402


class UiBackend:
    def invoke(self, operation, payload, *, timeout=None):
        if operation == "host_status":
            return {"hypervisor_present": True, "logical_processor_count": 12}
        if operation == "vm_get":
            return {"vm": {"name": "NORTH-Test", "state": "Running", "processor_count": 2, "memory_startup_bytes": 4 * 2**30, "automatic_checkpoints_enabled": False}}
        if operation == "checkpoint_list":
            return {"count": 1, "checkpoints": [{"id": "6f4b3f13-2a9f-4eb0-a3f4-bd52e890c001", "name": "before-lab", "creation_time": "2026-10-28T00:00:00Z"}]}
        if operation == "media_list":
            return {"dvd_drives": [{"controller_number": 0, "controller_location": 1, "mounted": True, "path": r"C:\ISOs\test.iso"}]}
        if operation == "debug_serial_status":
            return {"com_ports": [{"number": 1, "path": "", "debugger_mode": "On"}]}
        return {"operation": operation}

    def close(self):
        pass


def test_an_explicit_vm_wins_over_discovery() -> None:
    assert initial_vm_name("Chosen", frozenset(), ["Other", "Another"]) == "Chosen"


def test_a_single_discovered_vm_opens_with_no_configuration() -> None:
    """Zero-config: the host's only VM needs no --vm and no allowlist."""
    assert initial_vm_name(None, frozenset(), ["Only-VM"]) == "Only-VM"


def test_two_discovered_vms_ask_instead_of_guessing() -> None:
    assert initial_vm_name(None, frozenset(), ["One", "Two"]) is None
    assert initial_vm_name(None, frozenset(), []) is None


def test_the_allowlist_narrows_the_choice() -> None:
    """An allowlist of one still opens without a prompt, even on a busy host."""
    assert initial_vm_name(None, frozenset({"lab-vm"}), ["LAB-VM", "prod"]) == "LAB-VM"
    assert initial_vm_name(None, frozenset({"lab-vm"}), ["prod"]) is None


def test_the_debugger_page_shows_the_pipe_com1_is_already_on() -> None:
    """The port's own configuration outranks a name derived from the VM."""
    assert existing_pipe_name({"com_ports": [{"number": 1, "path": r"\\.\pipe\north-kd"}]}) == "north-kd"
    assert existing_pipe_name({"com_ports": [{"number": 1, "path": ""}]}) == ""
    assert existing_pipe_name({"com_ports": [{"number": 2, "path": r"\\.\pipe\other"}]}) == ""
    assert existing_pipe_name({}) == ""


def test_the_access_note_says_what_a_level_means_and_what_its_absence_does() -> None:
    from hyperv_console_mcp.dashboard import access_note

    assert "is lab" in access_note("NORTH-Test", {"north-test": "lab"})
    missing = access_note("NORTH-Test", {})
    assert "no level" in missing
    assert "no tool can reach it" in missing


@pytest.fixture
def window(tmp_path, monkeypatch):
    """A real window over a fake backend, closed however the test ends."""
    monkeypatch.setenv("HYPERV_MCP_EVENT_DIRECTORY", str(tmp_path / "events"))
    monkeypatch.setenv("HYPERV_MCP_SHARED_SERVICE", "false")
    monkeypatch.setattr(
        "hyperv_console_mcp.dashboard.create_backend", lambda *args, **kwargs: UiBackend()
    )
    try:
        app = Dashboard("NORTH-Test")
    except Exception as exc:
        if "display" in str(exc).casefold():
            pytest.skip("No interactive display is available.")
        raise
    try:
        yield app
    finally:
        app.close()


def _request(request_id: str = "req-1") -> dict:
    return {
        "request_id": request_id,
        "operation": "guest_exec",
        "vm_name": "NORTH-Test",
        "summary": {"command_preview": "hostname", "vm": "NORTH-Test"},
    }


def test_an_approval_is_a_card_and_never_takes_the_window_away(window) -> None:
    """The operator may need another page to decide; a grab would deny that."""
    answered: queue.Queue[bool] = queue.Queue(maxsize=1)
    window.pending["req-1"] = answered

    window._show_approval(_request())
    window.update()

    assert "req-1" in window.approvals.cards
    assert window.approvals.winfo_ismapped()
    assert window.grab_current() is None

    window._resolve_approval("req-1", True)
    window.update()

    assert answered.get_nowait() is True
    assert "req-1" not in window.approvals.cards
    assert not window.approvals.winfo_ismapped()


def test_the_queue_holds_several_requests_and_answers_only_the_one_clicked(window) -> None:
    first: queue.Queue[bool] = queue.Queue(maxsize=1)
    second: queue.Queue[bool] = queue.Queue(maxsize=1)
    window.pending.update({"req-1": first, "req-2": second})
    window._show_approval(_request("req-1"))
    window._show_approval(_request("req-2"))
    window.update()

    window._resolve_approval("req-2", False)
    window.update()

    assert second.get_nowait() is False
    assert first.empty()
    assert set(window.approvals.cards) == {"req-1"}


def test_a_card_whose_request_timed_out_leaves_the_queue(window) -> None:
    """The broker stops waiting on its own clock; the card must follow it."""
    window._show_approval(_request("stale"))
    window.update()
    assert "stale" in window.approvals.cards

    window._prune_approvals()
    window.update()

    assert window.approvals.cards == {}
    assert not window.approvals.winfo_ismapped()


def test_no_shipped_module_names_a_vm_or_a_pipe() -> None:
    """A default VM name and a default pipe name are guesses about the host."""
    root = Path(__file__).resolve().parents[1] / "src" / "hyperv_console_mcp"
    offenders = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "NORTH-Test" in path.read_text(encoding="utf-8")
        or "north-kd" in path.read_text(encoding="utf-8")
    )
    assert offenders == []


def test_dashboard_pages_render_with_fake_backend(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HYPERV_MCP_EVENT_DIRECTORY", str(tmp_path / "events"))
    # A private copy, because this test is about rendering: a shared host would
    # bypass the fake backend and talk to the machine the suite runs on.
    monkeypatch.setenv("HYPERV_MCP_SHARED_SERVICE", "false")
    monkeypatch.setattr(
        "hyperv_console_mcp.dashboard.create_backend", lambda *args, **kwargs: UiBackend()
    )
    try:
        app = Dashboard("NORTH-Test")
    except Exception as exc:
        if "display" in str(exc).casefold():
            pytest.skip("No interactive display is available.")
        raise
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            app.update()
            if not app.busy and app.state_card.get() != "Reading…":
                break
            time.sleep(0.01)
        assert app.busy == 0
        assert "NORTH-Test" in app.state_card.get()
        for page in ("Activity", "Driver lab", "Guest access", "Console", "Recovery", "Media", "Debugger", "Overview"):
            app.show(page)
            app.update()
        capture = os.environ.get("HYPERV_TEST_SCREENSHOT")
        if capture:
            from PIL import ImageGrab
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                app.update()
                if not app.busy and app.state_card.get() != "Reading…":
                    break
                time.sleep(0.01)
            app.update()
            bounds = (app.winfo_rootx(), app.winfo_rooty(), app.winfo_rootx() + app.winfo_width(), app.winfo_rooty() + app.winfo_height())
            ImageGrab.grab(bounds).save(Path(capture))
    finally:
        app.close()
