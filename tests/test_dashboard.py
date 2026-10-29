from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("tkinter", reason="the dashboard is a tkinter application")

from hyperv_console_mcp.dashboard import Dashboard  # noqa: E402


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


def test_dashboard_pages_render_with_fake_backend(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HYPERV_MCP_EVENT_DIRECTORY", str(tmp_path / "events"))
    monkeypatch.setattr("hyperv_console_mcp.dashboard.GuestSplitBackend", UiBackend)
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
        for page in ("Activity", "Driver lab", "Console", "Recovery", "Media", "Debugger", "Overview"):
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
