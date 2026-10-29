"""The kd.exe session driver, exercised against a fake debugger process."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from hyperv_console_mcp.errors import DebuggerError
from hyperv_console_mcp.kd.session import KdSession, is_go_command, run_dump

FAKE_KD = Path(__file__).with_name("fake_kd.py")


def start_session(tmp_path: Path, *extra: str, **kwargs) -> KdSession:
    session = KdSession(
        [sys.executable, str(FAKE_KD), *extra],
        log_path=tmp_path / "kd.log",
        vm_name="NORTH-Test",
        transport="kdnet",
        timeout=kwargs.pop("timeout", 5.0),
        **kwargs,
    )
    session.start()
    return session


@pytest.fixture
def session(tmp_path: Path):
    kd = start_session(tmp_path)
    try:
        yield kd
    finally:
        kd.close()


def test_start_lands_on_a_prompt(session: KdSession) -> None:
    assert session.state == "broken"
    assert session.status()["alive"] is True


def test_a_command_returns_only_its_own_output(session: KdSession) -> None:
    result = session.command("lm")

    assert "MyDriver" in result["output"]
    assert "Connected to target" not in result["output"]
    assert "kd>" not in result["output"]
    assert result["truncated"] is False
    assert result["state"] == "broken"


def test_markers_keep_consecutive_commands_apart(session: KdSession) -> None:
    first = session.command("lm")
    second = session.command("k")

    assert "MyDriver" in first["output"]
    assert "RetAddr" in second["output"]
    assert "start             end" not in second["output"]


def test_go_returns_when_the_target_breaks(session: KdSession) -> None:
    result = session.go("g")

    assert result["broke"] is True
    assert result["timed_out"] is False
    assert "Fatal System Error" in result["output"]
    assert session.state == "broken"


def test_commands_are_refused_while_the_target_runs(tmp_path: Path) -> None:
    session = start_session(tmp_path, "--scenario", "never_break", timeout=0.5)
    try:
        assert session.go("g")["timed_out"] is True
        assert session.state == "running"
        with pytest.raises(DebuggerError, match="running"):
            session.command("lm")
        broken = session.break_in()
        assert broken["broke"] is True
        assert session.state == "broken"
        assert "MyDriver" in session.command("lm")["output"]
    finally:
        session.close()


def test_break_in_on_a_broken_target_is_a_no_op(session: KdSession) -> None:
    result = session.break_in()

    assert result["already_broken"] is True
    assert result["broke"] is True


def test_a_hung_command_times_out_and_the_session_stays_usable(tmp_path: Path) -> None:
    session = start_session(tmp_path, timeout=0.5)
    try:
        with pytest.raises(DebuggerError, match="timed out") as error:
            session.command(".hang")
        assert error.value.code == "kd_command_timeout"
        # The sequence number in each marker keeps a late reply harmless.
        assert "MyDriver" in session.command("lm", timeout=5.0)["output"]
    finally:
        session.close()


def test_long_output_is_truncated_but_the_log_keeps_everything(tmp_path: Path) -> None:
    session = start_session(tmp_path, max_output_chars=1_024)
    try:
        result = session.command("big")
        assert result["truncated"] is True
        assert result["omitted_chars"] > 0
        assert "characters omitted" in result["output"]
        assert len(result["output"]) < 1_200
    finally:
        session.close()
    log = (tmp_path / "kd.log").read_text(encoding="utf-8")
    assert "A" * 4_000 in log


def test_wait_event_reports_a_module_load(tmp_path: Path) -> None:
    session = start_session(tmp_path, "--scenario", "module", timeout=0.1)
    try:
        assert session.go("g")["timed_out"] is True
        event = session.wait_event(timeout=3.0)
        assert event["event"] == "module_load"
        assert event["module"] == "MyDriver.sys"
    finally:
        session.close()


def test_wait_event_times_out_on_a_quiet_target(session: KdSession) -> None:
    event = session.wait_event(timeout=0.3)

    assert event["event"] == "timeout"
    assert event["state"] == "broken"


def test_detach_quits_the_debugger(session: KdSession) -> None:
    status = session.detach()

    assert status["state"] == "disconnected"
    assert status["alive"] is False


def test_an_exiting_debugger_marks_the_session_disconnected(tmp_path: Path) -> None:
    session = start_session(tmp_path, timeout=0.5)
    try:
        with pytest.raises(DebuggerError):
            session.command(".exit")
        deadline = time.monotonic() + 3
        while session.status()["alive"] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert session.status()["alive"] is False
        assert session.state == "disconnected"
    finally:
        session.close()


def test_status_never_reveals_the_command_line(tmp_path: Path) -> None:
    secret = "1.2.3.4"
    session = KdSession(
        [sys.executable, str(FAKE_KD), f"key={secret}"],
        log_path=tmp_path / "kd.log",
        vm_name="NORTH-Test",
        transport="kdnet",
        timeout=5.0,
    )
    session.start()
    try:
        assert secret not in str(session.status())
    finally:
        session.close()


def test_commands_must_be_single_line(session: KdSession) -> None:
    with pytest.raises(DebuggerError, match="single line"):
        session.command("lm\nqd")


def test_a_session_that_was_never_started_refuses_commands(tmp_path: Path) -> None:
    session = KdSession([sys.executable, str(FAKE_KD)], log_path=tmp_path / "kd.log")

    with pytest.raises(DebuggerError, match="No debugger session"):
        session.command("lm")


@pytest.mark.parametrize(
    ("command", "expected"),
    [("g", True), ("gh", True), ("gN", True), ("gu; k", True), ("gc 3", True),
     ("lm", False), ("bp nt!foo", False), ("", False)],
)
def test_go_commands_are_recognised_by_their_head(command: str, expected: bool) -> None:
    assert is_go_command(command) is expected


class FakeDumpProcess:
    def __init__(self, argv: list[str], **_: object) -> None:
        self.argv = argv
        self.returncode = 0

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        return b"BUGCHECK_CODE:  d1\n", b""


def test_a_dump_run_returns_the_output_and_exit_code(tmp_path: Path) -> None:
    dump = tmp_path / "MEMORY.DMP"
    dump.write_bytes(b"dump")
    captured: dict[str, list[str]] = {}

    def factory(argv: list[str], **kwargs: object) -> FakeDumpProcess:
        captured["argv"] = argv
        return FakeDumpProcess(argv, **kwargs)

    result = run_dump(
        "kd.exe", dump, commands=("!analyze -v",), process_factory=factory, timeout=30
    )

    assert captured["argv"] == ["kd.exe", "-z", str(dump), "-c", "!analyze -v; q"]
    assert result["exit_code"] == 0
    assert "BUGCHECK_CODE:  d1" in result["output"]


def test_a_missing_dump_is_reported_before_starting_kd(tmp_path: Path) -> None:
    with pytest.raises(DebuggerError, match="not found"):
        run_dump(sys.executable, tmp_path / "absent.dmp")
