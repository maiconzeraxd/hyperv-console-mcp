from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import PolicyError, ValidationError
from hyperv_console_mcp.service import HyperVService
from hyperv_console_mcp.transport import select_transport
from hyperv_console_mcp.transport.base import GuestTransport
from hyperv_console_mcp.transport.psdirect import PowerShellDirectTransport


@pytest.fixture(autouse=True)
def empty_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite must not depend on what this machine's Credential Manager holds."""
    monkeypatch.setattr(
        "hyperv_console_mcp.credentials._read", lambda target: None, raising=False
    )



class FakeBackend:
    """Mirrors the bridge envelopes the transport consumes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.files: dict[str, bytes] = {}

    def invoke(self, operation: str, payload: dict[str, Any], *, timeout=None):
        self.calls.append((operation, dict(payload)))
        if operation == "guest_exec":
            return {"vm": payload["vm"], "execution": {
                "success": True, "exit_code": 0, "output": "guest-name\r\n",
                "had_errors": False, "error_records": [], "output_truncated": False,
            }}
        if operation == "guest_status":
            return {"vm": payload["vm"], "guest": {"computer_name": "LAB"}}
        if operation == "guest_file_info":
            data = self.files[payload["path"]]
            return {"vm": payload["vm"], "file": {
                "path": payload["path"], "size": len(data), "status": "ok",
            }}
        if operation == "guest_put_file":
            content = Path(payload["local_path"]).read_bytes()
            self.files[payload["guest_path"]] = content
            digest = hashlib.sha256(content).hexdigest()
            return {"vm": payload["vm"], "file": {
                "path": payload["guest_path"], "size": len(content),
                "sha256": digest,
            }}
        if operation == "guest_get_file":
            content = self.files[payload["guest_path"]]
            target = Path(payload["local_path"])
            target.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            return {"vm": payload["vm"], "file": {
                "path": str(target), "size": len(content),
                "sha256": digest,
            }}
        if operation == "guest_write_file":
            content = base64.b64decode(payload["content_base64"])
            self.files[payload["path"]] = content
            return {"vm": payload["vm"], "file": {
                "path": payload["path"], "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }}
        return {"vm": payload.get("vm"), "operation": operation}


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "allowed_vms": frozenset({"NORTH-Test"}),
        "allow_guest": True,
        "guest_username": "Administrator",
        "guest_password": "secret",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def transport(backend: FakeBackend) -> PowerShellDirectTransport:
    return PowerShellDirectTransport(backend, settings())


def test_auto_and_explicit_psdirect_both_select_power_shell_direct(backend: FakeBackend) -> None:
    for choice in ("auto", "psdirect"):
        selected = select_transport(settings(guest_transport=choice), backend, "NORTH-Test")
        assert isinstance(selected, GuestTransport)
        assert selected.name == "psdirect"


def test_an_explicit_ssh_choice_never_falls_back_silently(backend: FakeBackend) -> None:
    with pytest.raises(PolicyError, match="guest_ssh_provision"):
        select_transport(settings(guest_transport="ssh"), backend, "NORTH-Test")


def test_a_per_vm_override_wins_over_the_default(backend: FakeBackend) -> None:
    # The override selects SSH for that VM only, so it demands provisioning
    # instead of quietly using the default transport.
    overrides = {"north-test": "ssh"}
    with pytest.raises(PolicyError, match="not provisioned"):
        select_transport(
            settings(guest_transport="auto", guest_transport_overrides=overrides),
            backend,
            "NORTH-Test",
        )


def test_exec_maps_the_bridge_envelope_and_names_its_transport(
    transport: PowerShellDirectTransport, backend: FakeBackend
) -> None:
    result = transport.exec("NORTH-Test", "hostname", timeout=5, max_output=1024)
    assert result.success is True
    assert result.exit_code == 0
    assert result.output == "guest-name\r\n"
    assert result.transport == "psdirect"
    assert backend.calls[-1][0] == "guest_exec"
    assert backend.calls[-1][1]["max_output_chars"] == 1024


def test_exec_without_credentials_fails_before_reaching_the_guest(backend: FakeBackend) -> None:
    transport = PowerShellDirectTransport(backend, settings(guest_password=None))
    with pytest.raises(PolicyError, match="HYPERV_MCP_GUEST_PASSWORD"):
        transport.exec("NORTH-Test", "hostname", timeout=5, max_output=1024)
    assert backend.calls == []


def test_put_file_sends_a_host_path_over_the_session_and_returns_the_guest_digest(
    transport: PowerShellDirectTransport, backend: FakeBackend, tmp_path: Path
) -> None:
    source = tmp_path / "driver.sys"
    source.write_bytes(b"MZ" + bytes(range(256)))
    result = transport.put_file("NORTH-Test", source, r"C:\DriverLab\driver.sys", overwrite=True)
    assert result.size == 258
    assert result.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert result.transport == "psdirect"
    operation, payload = backend.calls[-1]
    assert operation == "guest_put_file"
    assert payload["local_path"] == str(source)
    assert payload["guest_path"] == r"C:\DriverLab\driver.sys"
    assert payload["overwrite"] is True
    assert "content_base64" not in payload


def test_put_file_rejects_a_missing_host_file(
    transport: PowerShellDirectTransport, backend: FakeBackend, tmp_path: Path
) -> None:
    with pytest.raises(ValidationError, match="is not a file"):
        transport.put_file(
            "NORTH-Test", tmp_path / "absent.sys", r"C:\DriverLab\absent.sys", overwrite=False
        )
    assert backend.calls == []


def test_get_file_downloads_to_the_target_and_reports_the_verified_digest(
    transport: PowerShellDirectTransport, backend: FakeBackend, tmp_path: Path
) -> None:
    payload = bytes(range(256)) * 40
    backend.files[r"C:\DriverLab\dump.bin"] = payload
    target = tmp_path / "nested" / "dump.bin"
    result = transport.get_file("NORTH-Test", r"C:\DriverLab\dump.bin", target)
    assert target.read_bytes() == payload
    assert result.size == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.transport == "psdirect"
    assert backend.calls[-1][0] == "guest_get_file"
    assert backend.calls[-1][1]["local_path"] == str(target)


def test_every_guest_tool_result_names_its_transport(backend: FakeBackend, tmp_path: Path) -> None:
    service = HyperVService(backend, settings(transfer_root=tmp_path))
    assert service.guest_exec("NORTH-Test", "hostname")["execution"]["transport"] == "psdirect"
    assert service.guest_status("NORTH-Test")["transport"] == "psdirect"
    assert service.guest_processes("NORTH-Test")["transport"] == "psdirect"
    assert service.guest_read_file("NORTH-Test", r"C:\DriverLab\x.txt")["transport"] == "psdirect"


def test_guest_put_file_stays_inside_a_configured_root(
    backend: FakeBackend, tmp_path: Path
) -> None:
    """The four host roots are reachable; the rest of the host is not."""
    service = HyperVService(
        backend,
        settings(
            transfer_root=tmp_path / "transfers",
            lab_root=tmp_path / "lab",
            evidence_root=tmp_path / "evidence",
            iso_root=tmp_path / "media",
        ),
    )
    (tmp_path / "transfers").mkdir()
    (tmp_path / "lab").mkdir()
    inside = tmp_path / "transfers" / "driver.sys"
    inside.write_bytes(b"MZ")
    # A staged package lives under the lab root, and the file manager copies
    # from there; the widening is what makes that one transfer, not a detour.
    staged = tmp_path / "lab" / "package.sys"
    staged.write_bytes(b"MZ")
    outside = tmp_path / "outside.sys"
    outside.write_bytes(b"MZ")

    with pytest.raises(PolicyError, match="must remain under a configured root"):
        service.guest_put_file("NORTH-Test", str(outside), r"C:\DriverLab\driver.sys")
    assert backend.calls == []

    result = service.guest_put_file("NORTH-Test", str(inside), r"C:\DriverLab\driver.sys")
    assert result["file"]["size"] == 2
    assert result["file"]["transport"] == "psdirect"

    staged_result = service.guest_put_file(
        "NORTH-Test", str(staged), r"C:\DriverLab\package.sys"
    )
    assert staged_result["file"]["size"] == 2


def test_guest_get_file_refuses_to_overwrite_without_confirmation(
    backend: FakeBackend, tmp_path: Path
) -> None:
    service = HyperVService(backend, settings(transfer_root=tmp_path))
    backend.files[r"C:\DriverLab\dump.bin"] = b"payload"
    target = tmp_path / "dump.bin"
    target.write_bytes(b"previous")

    with pytest.raises(ValidationError, match="already exists"):
        service.guest_get_file("NORTH-Test", r"C:\DriverLab\dump.bin", str(target))
    assert target.read_bytes() == b"previous"

    result = service.guest_get_file(
        "NORTH-Test", r"C:\DriverLab\dump.bin", str(target),
        overwrite=True, confirm_path=str(target),
    )
    assert result["file"]["sha256"] == hashlib.sha256(b"payload").hexdigest()
    assert target.read_bytes() == b"payload"
