"""The service-level kd tools: gating, transport choice, and secret hygiene."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import DebuggerError, PolicyError, ValidationError
from hyperv_console_mcp.kd import kdnet
from hyperv_console_mcp.service import HyperVService

ANALYZE_TEXT = """\
BUGCHECK_CODE:  d1
BUGCHECK_P1: ffff8000`00001000
BUGCHECK_P2: 2
BUGCHECK_P3: 0
BUGCHECK_P4: fffff800`12345678
Probably caused by : MyDriver.sys ( MyDriver!Crash+18 )
FAILURE_BUCKET_ID:  0xD1_MyDriver!Crash
MODULE_NAME: MyDriver
IMAGE_NAME:  MyDriver.sys
"""


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, dict[str, Any]] = {}

    def invoke(
        self, operation: str, payload: Mapping[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((operation, dict(payload)))
        return self.responses.get(operation, {"operation": operation})


class FakeSerial:
    def __init__(self, connected: bool = False) -> None:
        self.connected = connected
        self.disconnected = False

    def status(self, vm: str) -> dict[str, Any]:
        return {"connected": self.connected}

    def disconnect(self, vm: str) -> dict[str, Any]:
        self.connected = False
        self.disconnected = True
        return {"connected": False, "completed": True}


class FakeSession:
    def __init__(self, argv: list[str] | None = None, log_path: str = "") -> None:
        self.argv = argv or []
        self.log_path = log_path
        self.state = "broken"
        self.breaks = 0
        self.commands: list[str] = []

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state, "alive": True, "pid": 4_242,
            "transport": "kdnet", "log_path": self.log_path,
        }

    def break_in(self, *, timeout: float = 15.0) -> dict[str, Any]:
        self.breaks += 1
        self.state = "broken"
        return {"requested": True, "broke": True, "state": self.state}

    def command(self, text: str, *, timeout: float | None = None) -> dict[str, Any]:
        self.commands.append(text)
        return {"command": text, "output": ANALYZE_TEXT, "state": self.state}

    def go(self, command: str = "g", *, timeout: float | None = None) -> dict[str, Any]:
        return {"command": command, "output": "", "broke": False, "state": "running"}

    def wait_event(self, *, timeout: float = 60.0) -> dict[str, Any]:
        return {"event": "break", "module": None, "state": "broken", "output": ""}

    def detach(self, *, timeout: float = 10.0) -> dict[str, Any]:
        self.state = "disconnected"
        return self.status()

    def close(self) -> None:
        self.state = "disconnected"


class FakeKd:
    def __init__(self) -> None:
        self.sessions: dict[str, FakeSession] = {}
        self.argv: dict[str, list[str]] = {}
        self.startup_timeouts: list[float | None] = []

    def get(self, vm: str) -> FakeSession | None:
        return self.sessions.get(vm.casefold())

    def require(self, vm: str) -> FakeSession:
        session = self.get(vm)
        if session is None:
            raise DebuggerError("none", code="kd_not_connected")
        return session

    def start(self, vm: str, create: Any, *, startup_timeout: float | None = None) -> FakeSession:
        self.startup_timeouts.append(startup_timeout)
        real = create()
        # The real session is never started here; its argv is what we assert on.
        self.argv[vm.casefold()] = list(real._command)
        log_path = str(real.log_path)
        real.close()
        session = FakeSession(self.argv[vm.casefold()], log_path)
        self.sessions[vm.casefold()] = session
        return session

    def stop(self, vm: str, *, detach: bool = True) -> dict[str, Any]:
        session = self.sessions.pop(vm.casefold(), None)
        if session is None:
            return {"attached": False, "completed": True}
        session.state = "disconnected"
        return {"attached": True, "completed": True, **session.status()}


class FakeStore:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})

    def read(self, key: str) -> str | None:
        return self.values.get(key)

    def write(self, key: str, secret: str, *, label: str = "kdnet") -> None:
        self.values[key] = secret

    def delete(self, key: str) -> bool:
        return self.values.pop(key, None) is not None

    def has(self, key: str) -> bool:
        return key in self.values


@pytest.fixture
def service(tmp_path: Path) -> HyperVService:
    backend = FakeBackend()
    backend.responses["debug_serial_status"] = {
        "vm": "NORTH-Test",
        "state": "Running",
        "com_ports": [{"number": 1, "path": r"\\.\pipe\hvmcp-serial"}],
    }
    instance = HyperVService(
        backend,
        Settings(
            allowed_vms=frozenset({"NORTH-Test"}),
            allow_debugger=True,
            kd_log_root=tmp_path / "kd",
            symbol_root=tmp_path / "symbols",
            lab_root=tmp_path / "lab",
            evidence_root=tmp_path / "evidence",
            debuggers_dir=tmp_path,
        ),
    )
    instance.kd = FakeKd()  # type: ignore[assignment]
    return instance


@pytest.fixture
def kdnet_state(service: HyperVService, tmp_path: Path) -> dict[str, Any]:
    state = {
        "vm": "NORTH-Test",
        "port": 50_000,
        "host_ip": "10.77.0.1",
        "switch_name": "HyperV-MCP-Lab",
        "adapter_name": "HyperV-MCP-Lab",
        "firewall_rule": "HyperVConsoleMCP KDNET NORTH-Test",
        "guest_dbgsettings": "net ... key ***",
    }
    kdnet.write_json(kdnet.kdnet_state_path(service.settings, "NORTH-Test"), state)
    return state


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    store = FakeStore({"NORTH-Test": "1.2.3.4"})
    monkeypatch.setattr(kdnet, "key_store", lambda: store)
    monkeypatch.setattr(
        "hyperv_console_mcp.service.find_debugger",
        lambda *args, **kwargs: Path("C:/Debuggers/kd.exe"),
    )
    return store


def test_attach_requires_prepared_kdnet_state(service: HyperVService, patched: FakeStore) -> None:
    with pytest.raises(PolicyError):
        service.kd_attach("NORTH-Test", "kdnet")


def test_attach_requires_the_key_in_the_credential_manager(
    service: HyperVService, kdnet_state: dict[str, Any], patched: FakeStore
) -> None:
    patched.values.clear()
    with pytest.raises(PolicyError):
        service.kd_attach("NORTH-Test", "kdnet")


def test_attach_builds_the_kdnet_command_line_and_hides_the_key(
    service: HyperVService, kdnet_state: dict[str, Any], patched: FakeStore
) -> None:
    result = service.kd_attach("NORTH-Test", "kdnet")

    argv = service.kd.argv["north-test"]  # type: ignore[attr-defined]
    assert argv[0].endswith("kd.exe")
    assert argv[1] == "-k"
    assert argv[2] == "net:port=50000,key=1.2.3.4"
    assert "1.2.3.4" not in str(result)
    assert result["attached"] is True
    assert result["log_path"].endswith(".log")
    assert "1.2.3.4" not in Path(result["log_path"]).name
    assert result["serial_detached"] is False


def test_attach_over_serial_detaches_the_console_reader(
    service: HyperVService, patched: FakeStore
) -> None:
    service.serial = FakeSerial(connected=True)  # type: ignore[assignment]

    result = service.kd_attach("NORTH-Test", "serial")

    argv = service.kd.argv["north-test"]  # type: ignore[attr-defined]
    assert argv[2] == r"com:pipe,port=\\.\pipe\hvmcp-serial,baud=115200,reconnect,resets=0"
    assert service.serial.disconnected is True  # type: ignore[attr-defined]
    assert result["serial_detached"] is True


def test_serial_connect_refuses_while_kd_owns_the_pipe(service: HyperVService) -> None:
    service.kd.sessions["north-test"] = FakeSession()  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        service.serial_connect(
            "NORTH-Test", "hvmcp-serial", r"C:\logs\serial.log", "NORTH-Test"
        )


def test_status_without_a_session(service: HyperVService) -> None:
    result = service.kd_status("NORTH-Test")
    assert result["attached"] is False
    assert result["serial"] == {"connected": False}


def test_break_falls_back_to_an_nmi(service: HyperVService) -> None:
    session = FakeSession()
    session.state = "running"
    service.kd.sessions["north-test"] = session  # type: ignore[attr-defined]

    def break_in(*, timeout: float = 15.0) -> dict[str, Any]:
        session.breaks += 1
        if session.breaks == 1:
            raise DebuggerError("break failed", code="kd_break_failed")
        session.state = "broken"
        return {"requested": True, "broke": True, "state": "broken"}

    session.break_in = break_in  # type: ignore[method-assign]
    result = service.kd_break("NORTH-Test")

    assert result["method"] == "nmi"
    assert ("vm_inject_nmi", {"vm": "NORTH-Test"}) in service.backend.calls  # type: ignore[attr-defined]


def test_break_without_the_nmi_fallback_propagates(service: HyperVService) -> None:
    session = FakeSession()
    session.state = "running"

    def break_in(*, timeout: float = 15.0) -> dict[str, Any]:
        raise DebuggerError("break failed", code="kd_break_failed")

    session.break_in = break_in  # type: ignore[method-assign]
    service.kd.sessions["north-test"] = session  # type: ignore[attr-defined]

    with pytest.raises(DebuggerError):
        service.kd_break("NORTH-Test", nmi=False)
    assert not any(
        operation == "vm_inject_nmi" for operation, _ in service.backend.calls  # type: ignore[attr-defined]
    )


def test_command_rejects_multiline_input(service: HyperVService) -> None:
    service.kd.sessions["north-test"] = FakeSession()  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        service.kd_command("NORTH-Test", "lm\n!analyze -v")


def test_analyze_parses_the_report(service: HyperVService) -> None:
    service.kd.sessions["north-test"] = FakeSession()  # type: ignore[attr-defined]
    result = service.kd_analyze("NORTH-Test")
    report = result["report"]
    assert report["found"] is True
    assert report["bugcheck_code"] == "0xd1"
    assert report["blamed_driver"] == "MyDriver.sys"


def test_set_breakpoint_builds_bp_and_bu(service: HyperVService) -> None:
    session = FakeSession()
    service.kd.sessions["north-test"] = session  # type: ignore[attr-defined]

    service.kd_set_breakpoint("NORTH-Test", "MyDriver!Init")
    service.kd_set_breakpoint("NORTH-Test", "MyDriver!Init", deferred=True)

    assert session.commands == ["bp MyDriver!Init", "bu MyDriver!Init"]


def test_set_breakpoint_rejects_a_multi_token_target(service: HyperVService) -> None:
    service.kd.sessions["north-test"] = FakeSession()  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        service.kd_set_breakpoint("NORTH-Test", "bp nt!X; g")


def test_break_on_load_builds_sxe_and_sxd(service: HyperVService) -> None:
    session = FakeSession()
    service.kd.sessions["north-test"] = session  # type: ignore[attr-defined]
    service.kd_break_on_load("NORTH-Test", "MyDriver")
    service.kd_break_on_load("NORTH-Test", "MyDriver", enabled=False)
    assert session.commands == ["sxe ld:MyDriver", "sxd ld:MyDriver"]


def test_dump_open_confines_the_path(service: HyperVService, tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        service.kd_dump_open("NORTH-Test", str(tmp_path / "outside.dmp"))
    with pytest.raises(ValidationError):
        service.kd_dump_open(
            "NORTH-Test", str(Path(service.settings.evidence_root) / "notes.txt")
        )


def test_dump_open_runs_kd_and_parses(
    service: HyperVService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = Path(service.settings.evidence_root)
    evidence.mkdir(parents=True)
    dump = evidence / "MEMORY.dmp"
    dump.write_bytes(b"dump")
    captured: dict[str, Any] = {}

    def fake_run(kd_path: Any, dump_path: Any, **kwargs: Any) -> dict[str, Any]:
        captured["argv"] = (str(kd_path), str(dump_path), kwargs["commands"])
        return {"dump": str(dump_path), "exit_code": 0, "output": ANALYZE_TEXT}

    monkeypatch.setattr("hyperv_console_mcp.service.run_dump", fake_run)

    result = service.kd_dump_open("NORTH-Test", str(dump))

    assert captured["argv"][2] == ["!analyze -v"]
    assert result["report"]["bugcheck_code"] == "0xd1"


def test_kd_tools_stay_inside_the_allowlist(service: HyperVService) -> None:
    with pytest.raises(PolicyError):
        service.kd_status("Other")


def test_prepare_refuses_while_a_session_is_attached(service: HyperVService) -> None:
    service.kd.sessions["north-test"] = FakeSession()  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        service.debug_kdnet_prepare("NORTH-Test", "NORTH-Test")


def test_symbol_path_has_the_public_server_and_a_lab_package(
    service: HyperVService, tmp_path: Path
) -> None:
    package = Path(service.settings.lab_root) / "mydriver"
    package.mkdir(parents=True)
    (package / "MyDriver.pdb").write_bytes(b"pdb")

    symbol_path = service._symbol_path()

    entries = symbol_path.split(";")
    assert entries[0].startswith("srv*")
    assert "https://msdl.microsoft.com/download/symbols" in entries[0]
    assert str(package) in entries
