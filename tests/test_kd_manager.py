"""KdManager: one live session per VM, a clean rollback, and a clean exit."""

from __future__ import annotations

from typing import Any

import pytest

from hyperv_console_mcp.errors import DebuggerError
from hyperv_console_mcp.kd.manager import KdManager


class FakeSession:
    def __init__(self, fail_start: bool = False) -> None:
        self.state = "broken"
        self.started = False
        self.closed = False
        self.detached = False
        self.startup_timeout: float | None = None
        self.fail_start = fail_start

    @property
    def alive(self) -> bool:
        return self.state != "disconnected"

    def start(self, *, startup_timeout: float = 60.0) -> dict[str, Any]:
        self.startup_timeout = startup_timeout
        if self.fail_start:
            raise DebuggerError("no target", code="kd_connect_timeout")
        self.started = True
        return {"state": self.state}

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "alive": self.alive}

    def detach(self, *, timeout: float = 10.0) -> dict[str, Any]:
        self.detached = True
        self.state = "disconnected"
        return self.status()

    def close(self) -> None:
        self.closed = True
        self.state = "disconnected"


def test_require_names_the_missing_session() -> None:
    manager = KdManager()
    with pytest.raises(DebuggerError) as error:
        manager.require("NORTH-Test")
    assert error.value.code == "kd_not_connected"


def test_start_is_case_insensitive_and_refuses_to_shadow_a_live_session() -> None:
    manager = KdManager()
    first = FakeSession()
    manager.start("NORTH-Test", lambda: first)
    assert manager.get("north-test") is first
    with pytest.raises(DebuggerError) as error:
        manager.start("north-test", FakeSession)
    assert error.value.code == "kd_already_connected"
    manager.close()


def test_a_dead_session_is_replaced() -> None:
    manager = KdManager()
    dead = FakeSession()
    dead.state = "disconnected"
    manager.start("NORTH-Test", lambda: dead)
    live = FakeSession()
    manager.start("NORTH-Test", lambda: live)
    assert manager.get("NORTH-Test") is live
    assert dead.closed is True
    manager.close()


def test_a_failed_start_leaves_no_session_behind() -> None:
    manager = KdManager()
    with pytest.raises(DebuggerError):
        manager.start("NORTH-Test", lambda: FakeSession(fail_start=True))
    assert manager.get("NORTH-Test") is None


def test_start_forwards_the_startup_timeout() -> None:
    manager = KdManager()
    session = FakeSession()
    manager.start("NORTH-Test", lambda: session, startup_timeout=12.5)
    assert session.startup_timeout == 12.5
    manager.close()


def test_stop_detaches_by_default_and_reports_status() -> None:
    manager = KdManager()
    session = FakeSession()
    manager.start("NORTH-Test", lambda: session)
    result = manager.stop("NORTH-Test")
    assert session.detached is True
    assert result["attached"] is True
    assert result["completed"] is True
    assert manager.get("NORTH-Test") is None


def test_stop_of_an_unknown_vm_is_harmless() -> None:
    assert KdManager().stop("NORTH-Test") == {"attached": False, "completed": True}


def test_close_closes_every_session() -> None:
    manager = KdManager()
    first, second = FakeSession(), FakeSession()
    manager.start("A", lambda: first)
    manager.start("B", lambda: second)
    manager.close()
    assert first.closed and second.closed
