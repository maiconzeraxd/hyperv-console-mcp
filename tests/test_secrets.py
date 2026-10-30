"""No secret this server handles reaches a result, an event, an audit line or a log.

The Definition of Done in ``docs/ROADMAP-v0.2.md`` asks for one automated test
that looks for the test secret values in every output the server produces. Each
scenario below drives the real code path with fakes at the host and guest edges,
then the same scan runs over the tool results, the daily JSONL event log, the
audit log, the ``kd.exe`` session log and every state file under the roots the
scenario writes to.
"""

from __future__ import annotations

import ipaddress
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import hyperv_console_mcp.service as service_module
from hyperv_console_mcp import events as events_module
from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import PolicyError
from hyperv_console_mcp.kd import kdnet
from hyperv_console_mcp.kd.session import KdSession
from hyperv_console_mcp.service import HyperVService
from hyperv_console_mcp.transport import ssh as ssh_module
from hyperv_console_mcp.transport import ssh_setup

VM = "NORTH-Test"
FAKE_KD = Path(__file__).with_name("fake_kd.py")

PASSWORD = "Hvmcp-Test-Pass-2f9c41"
KDNET_KEY = "101.102.103.104"
SSH_KEY_MARKER = "HVMCPTESTPRIVATEKEY7d31"
SSH_PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    f"{SSH_KEY_MARKER}\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)
SECRETS = (PASSWORD, KDNET_KEY, SSH_KEY_MARKER)


# -- scanning ---------------------------------------------------------------


def assert_clean(label: str, value: Any) -> None:
    """Fail when any test secret appears in a result or a piece of text."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    for secret in SECRETS:
        assert secret not in text, f"a test secret appeared in {label}"


def assert_tree_clean(root: Path, *, allow: tuple[Path, ...] = ()) -> None:
    """Scan every file a scenario wrote, except files that must hold the secret."""
    for path in root.rglob("*"):
        if not path.is_file() or path in allow:
            continue
        assert_clean(str(path), path.read_text(encoding="utf-8", errors="replace"))


def assert_audit_clean(settings: Settings) -> None:
    path = settings.audit_log
    assert path is not None and path.is_file()
    assert_clean("the audit log", path.read_text(encoding="utf-8"))


def assert_events_clean(settings: Settings) -> None:
    directory = Path(settings.event_directory)
    files = list(directory.glob("events-*.jsonl")) if directory.is_dir() else []
    assert files, "the scenario was supposed to publish events"
    for path in files:
        assert_clean(str(path), path.read_text(encoding="utf-8"))


# -- fakes ------------------------------------------------------------------


class FakeStore:
    """Stands in for the Windows Credential Manager; nothing touches the vault."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def read(self, key: str) -> str | None:
        return self.values.get(key)

    def write(self, key: str, secret: str, *, label: str = "kdnet") -> None:
        self.values[key] = secret

    def delete(self, key: str) -> bool:
        return self.values.pop(key, None) is not None

    def has(self, key: str) -> bool:
        return key in self.values


class FakeGuestTransport:
    """PowerShell Direct, reduced to what KDNET setup and SSH status touch."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def health(self, vm: str) -> dict[str, Any]:
        return {"vm": vm, "healthy": True}

    def exec(self, vm: str, command: str, *, timeout: float, max_output: int):
        return SimpleNamespace(
            output=kdnet.OK_MARKER + "net ... key *** | debug Yes", success=True
        )


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke(self, operation: str, payload: dict[str, Any], *, timeout: float | None = None):
        self.calls.append((operation, dict(payload)))
        if operation == "ssh_network_prepare":
            return {
                "adapter_name": "HyperV-MCP-Lab",
                "adapter_created": True,
                "host_address_created": True,
            }
        if operation == "kdnet_firewall_apply":
            return {"rule_name": payload["rule_name"], "created": True}
        if operation == "kdnet_firewall_remove":
            return {"removed": True}
        if operation == "guest_exec":
            return {"vm": payload["vm"], "execution": {
                "success": True, "exit_code": 0, "output": "LAB\r\n",
                "had_errors": False, "error_records": [],
            }}
        return {"vm": payload.get("vm")}

    def close(self) -> None:
        pass


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("HYPERV_MCP_EVENT_DIRECTORY", str(tmp_path / "events"))
    monkeypatch.delenv("HYPERV_MCP_REQUIRE_DASHBOARD_APPROVAL", raising=False)
    monkeypatch.delenv("HYPERV_MCP_PROFILE", raising=False)
    return Settings(
        allowed_vms=frozenset({VM}),
        allow_guest=True,
        allow_debugger=True,
        guest_username="labuser",
        guest_password=PASSWORD,
        kd_log_root=tmp_path / "kd",
        symbol_root=tmp_path / "symbols",
        ssh_root=tmp_path / "ssh",
        evidence_root=tmp_path / "evidence",
        audit_log=tmp_path / "audit.jsonl",
        # The field default is the user's own directory, not the environment
        # variable, so a scan that only sets the variable would read and write
        # the real one. This fixture must never touch anything outside tmp_path.
        event_directory=tmp_path / "events",
    )


# -- KDNET ------------------------------------------------------------------


def test_kdnet_setup_keeps_the_key_out_of_every_output(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeStore()
    monkeypatch.setattr(kdnet, "generate_key", lambda: KDNET_KEY)
    monkeypatch.setattr(kdnet, "key_store", lambda: store)
    monkeypatch.setattr(kdnet, "find_debugger", lambda *a, **k: Path("C:/Debuggers/kd.exe"))
    monkeypatch.setattr(
        kdnet,
        "host_address",
        lambda s: (ipaddress.ip_network("10.77.0.0/24"), "10.77.0.1"),
    )
    monkeypatch.setattr(kdnet, "PowerShellDirectTransport", FakeGuestTransport)

    service = HyperVService(FakeBackend(), settings)
    prepared = service.debug_kdnet_prepare(VM, VM)
    status = service.debug_kdnet_status(VM)
    removed = service.debug_kdnet_deprovision(VM, VM)

    # The key really was generated and handled; the assertions below would be
    # meaningless if it had never existed.
    assert store.values == {}
    assert prepared["port"] == kdnet.PORT_MIN

    for label, value in (
        ("debug_kdnet_prepare", prepared),
        ("debug_kdnet_status", status),
        ("debug_kdnet_deprovision", removed),
    ):
        assert_clean(label, value)
    assert_audit_clean(settings)


def test_the_kdnet_state_file_never_stores_the_key(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeStore()
    monkeypatch.setattr(kdnet, "generate_key", lambda: KDNET_KEY)
    monkeypatch.setattr(kdnet, "key_store", lambda: store)
    monkeypatch.setattr(kdnet, "find_debugger", lambda *a, **k: Path("C:/Debuggers/kd.exe"))
    monkeypatch.setattr(
        kdnet,
        "host_address",
        lambda s: (ipaddress.ip_network("10.77.0.0/24"), "10.77.0.1"),
    )
    monkeypatch.setattr(kdnet, "PowerShellDirectTransport", FakeGuestTransport)

    HyperVService(FakeBackend(), settings).debug_kdnet_prepare(VM, VM)

    state = kdnet.kdnet_state_path(settings, VM)
    assert json.loads(state.read_text(encoding="utf-8"))["port"] == kdnet.PORT_MIN
    assert_clean("kdnet.json", state.read_text(encoding="utf-8"))
    assert_tree_clean(Path(settings.kd_log_root))


# -- kd.exe session ---------------------------------------------------------


def fake_session_class(recorded: list[list[str]]) -> type:
    """A KdSession that runs fake_kd.py but keeps the argv kd_attach built."""

    class FakeKdSession:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            recorded.append(list(command))
            self._real = KdSession([sys.executable, str(FAKE_KD), *command[1:]], **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

    return FakeKdSession


def test_the_kd_command_line_never_reaches_a_result_or_the_session_log(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeStore()
    store.write(VM, KDNET_KEY)
    kdnet.write_json(
        kdnet.kdnet_state_path(settings, VM),
        {"vm": VM, "port": 50_007, "host_ip": "10.77.0.1"},
    )
    monkeypatch.setattr(kdnet, "key_store", lambda: store)
    monkeypatch.setattr(
        service_module, "find_debugger", lambda *a, **k: Path("C:/Debuggers/kd.exe")
    )
    recorded: list[list[str]] = []
    monkeypatch.setattr(service_module, "KdSession", fake_session_class(recorded))

    service = HyperVService(FakeBackend(), settings)
    attached = service.kd_attach(VM)
    try:
        # The key is in the debugger's command line, as KDNET requires.
        assert recorded[0][-1] == f"net:port=50007,key={KDNET_KEY}"
        assert attached["attached"] is True

        assert_clean("kd_attach", attached)
        assert_clean("kd_status", service.kd_status(VM))
        assert_clean("kd_command", service.kd_command(VM, "lm"))
        assert_clean("kd_stack", service.kd_stack(VM))
        assert_clean("kd_modules", service.kd_modules(VM))
    finally:
        service.kd_detach(VM)

    # The session log holds everything kd printed; the key is not in it.
    assert_tree_clean(Path(settings.kd_log_root))
    assert_audit_clean(settings)


# -- guest credential -------------------------------------------------------


def test_a_guest_command_never_carries_the_password_into_events_or_audit(
    settings: Settings,
) -> None:
    service = HyperVService(FakeBackend(), settings)

    # The MCP server wraps bridge calls in an event operation; do the same here
    # so the daily JSONL log is actually written and scanned.
    with events_module.EVENTS.operation("guest_exec", VM):
        result = service.guest_exec(VM, "hostname")

    assert result["execution"]["success"] is True
    assert_clean("guest_exec", result)
    assert_audit_clean(settings)
    assert_events_clean(settings)


def test_the_credential_status_reports_the_source_without_the_secret(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyperv_console_mcp import credentials

    # The real vault is never opened by this test.
    monkeypatch.setattr(
        credentials.CredentialStore, "has", lambda self, vm: False, raising=False
    )
    monkeypatch.setattr(
        credentials.CredentialStore, "read", lambda self, vm: None, raising=False
    )
    service = HyperVService(FakeBackend(), settings)

    status = service.guest_credential_status(VM)

    assert status["source"] == "environment"
    assert_clean("guest_credential_status", status)


# -- SSH --------------------------------------------------------------------


def test_the_ssh_private_key_stays_in_its_file(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = Path(settings.ssh_root) / VM
    directory.mkdir(parents=True)
    key_path = directory / "id_ed25519"
    key_path.write_text(SSH_PRIVATE_KEY, encoding="utf-8")
    (directory / "state.json").write_text(
        json.dumps({"host": "10.77.0.12", "port": 22, "adapter_name": "lab"}), encoding="utf-8"
    )
    (directory / "known_hosts").write_text(
        "[10.77.0.12]:22 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample\n", encoding="utf-8"
    )

    def no_paramiko() -> Any:
        raise PolicyError("The SSH transport needs paramiko.")

    monkeypatch.setattr(ssh_module, "_paramiko", no_paramiko)
    monkeypatch.setattr(ssh_setup, "PowerShellDirectTransport", FakeGuestTransport)

    service = HyperVService(FakeBackend(), settings)
    status = service.guest_ssh_status(VM)

    assert status["provisioned"] is True
    assert_clean("guest_ssh_status", status)
    # Everything except the key file itself must be clean.
    assert_tree_clean(Path(settings.ssh_root), allow=(key_path,))
    assert_audit_clean(settings)


# -- the scan, as a negative control ---------------------------------------


class LeakingService(HyperVService):
    """The defect this file exists to catch: a result that carries the secret.

    Nothing else about the build changes, so a scanner that stays green against
    it is a scanner that cannot fail, and every assertion above is decoration.
    """

    def debug_kdnet_prepare(self, vm_name: str, confirm_vm: str, **kwargs: Any):
        prepared = super().debug_kdnet_prepare(vm_name, confirm_vm, **kwargs)
        prepared["key"] = PASSWORD
        return prepared


def test_the_scan_turns_red_on_a_build_that_leaks(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeStore()
    monkeypatch.setattr(kdnet, "generate_key", lambda: KDNET_KEY)
    monkeypatch.setattr(kdnet, "key_store", lambda: store)
    monkeypatch.setattr(kdnet, "find_debugger", lambda *a, **k: Path("C:/Debuggers/kd.exe"))
    monkeypatch.setattr(
        kdnet,
        "host_address",
        lambda s: (ipaddress.ip_network("10.77.0.0/24"), "10.77.0.1"),
    )
    monkeypatch.setattr(kdnet, "PowerShellDirectTransport", FakeGuestTransport)

    prepared = LeakingService(FakeBackend(), settings).debug_kdnet_prepare(VM, VM)

    # The same check the real scenario runs, against the same kind of result.
    with pytest.raises(AssertionError, match="a test secret appeared"):
        assert_clean("debug_kdnet_prepare", prepared)


def test_every_scanner_fails_on_the_file_it_is_meant_to_read(
    settings: Settings,
) -> None:
    """One leak per scanner, so none of the four can pass by not looking."""
    root = Path(settings.evidence_root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "collected.txt").write_text(f"password={PASSWORD}\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="a test secret appeared"):
        assert_tree_clean(root)

    directory = Path(settings.event_directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "events-2026-01-01.jsonl").write_text(
        json.dumps({"operation": "guest_exec", "detail": SSH_KEY_MARKER}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="a test secret appeared"):
        assert_events_clean(settings)

    assert settings.audit_log is not None
    settings.audit_log.write_text(
        json.dumps({"operation": "kd_attach", "net": f"key={KDNET_KEY}"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="a test secret appeared"):
        assert_audit_clean(settings)
