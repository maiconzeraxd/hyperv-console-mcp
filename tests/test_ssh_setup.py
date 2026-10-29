"""Host-side SSH provisioning: addresses, keys, state, and the sequence.

The guest is modelled by a fake bridge here; nothing in this file touches a VM.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import BackendError
from hyperv_console_mcp.transport import ssh_setup
from hyperv_console_mcp.transport.ssh import ERROR_MARKER, JSON_MARKER, vm_key

VM = "LAB-Test"


def settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "allowed_vms": frozenset({VM}),
        "allow_guest": True,
        "guest_username": "mcn1",
        "guest_password": "not-a-real-secret",
        "ssh_root": tmp_path / "ssh",
        "transfer_root": tmp_path / "transfers",
    }
    values.update(overrides)
    return Settings(**values)


class FakeBackend:
    """Answers the bridge operations provisioning uses."""

    def __init__(self, guest_output: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.guest_output = guest_output
        self.host_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyMaterialForTests"

    def invoke(self, operation: str, payload: dict[str, Any], *, timeout=None):
        self.calls.append((operation, dict(payload)))
        if operation == "guest_status":
            return {"vm": payload["vm"], "guest": {"computer_name": "LAB"}}
        if operation == "vm_get":
            return {"vm": {"name": payload["vm"], "adapters": [
                {"name": "HyperV-MCP-Lab", "switch_name": "HyperV-MCP-Lab",
                 "mac_address": "00155D000001"},
            ]}}
        if operation == "ssh_network_prepare":
            return {
                "vm": payload["vm"], "switch_name": payload["switch_name"],
                "switch_created": True, "adapter_name": "HyperV-MCP-Lab",
                "adapter_created": True, "host_ip": payload["host_ip"],
                "host_address_created": True,
            }
        if operation == "ssh_network_remove":
            return {"vm": payload["vm"], "switch_name": payload["switch_name"],
                    "removed": ["adapter", "host_address"]}
        if operation == "guest_put_file":
            content = Path(payload["local_path"]).read_bytes()
            return {"vm": payload["vm"], "file": {
                "path": payload["guest_path"], "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }}
        if operation == "guest_exec":
            output = self.guest_output
            if output is None:
                body = json.dumps({
                    "mode": "Install", "sshd_source": "capability",
                    "host_key_public": self.host_key,
                    "state": {"sshd_status": "Running"},
                })
                output = JSON_MARKER + base64.b64encode(body.encode()).decode()
            return {"vm": payload["vm"], "execution": {
                "success": True, "exit_code": 0, "output": output,
                "had_errors": False, "error_records": [], "output_truncated": False,
            }}
        raise AssertionError(f"unexpected bridge operation: {operation}")


class FakeService:
    def __init__(self, backend: FakeBackend, settings_value: Settings) -> None:
        self.backend = backend
        self.settings = settings_value

    def _run(self, operation, bridge_operation, payload=None, *, vm_name=None, audit=True):
        return self.backend.invoke(bridge_operation, payload or {})


def test_the_host_and_guest_addresses_stay_inside_the_lab_subnet(tmp_path: Path) -> None:
    network, host = ssh_setup.host_address(settings(tmp_path))
    assert host == "10.77.0.1"
    assert network.prefixlen == 24
    guest = ssh_setup.guest_address(settings(tmp_path), VM)
    assert guest != host
    assert ipaddress.ip_address(guest) in network
    # Stable across calls, so a re-provision does not move the guest.
    assert ssh_setup.guest_address(settings(tmp_path), VM) == guest
    assert ssh_setup.guest_address(settings(tmp_path), "other-vm") != guest


def test_the_keypair_is_open_ssh_format_and_stable(tmp_path: Path) -> None:
    directory = tmp_path / "ssh" / vm_key(VM)
    public = ssh_setup.ensure_keypair(directory)
    assert public.startswith("ssh-ed25519 ")
    private = (directory / "id_ed25519").read_text(encoding="utf-8")
    assert private.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert ssh_setup.ensure_keypair(directory) == public


def test_provision_pins_the_host_key_and_records_the_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend()
    service = FakeService(backend, settings(tmp_path))
    monkeypatch.setattr(ssh_setup, "_probe", lambda settings_value, vm: {"ok": True, "error": None})

    result = ssh_setup.provision(service, VM)

    directory = tmp_path / "ssh" / vm_key(VM)
    state = json.loads((directory / "state.json").read_text())
    known_hosts = (directory / "known_hosts").read_text()
    assert result["verified"] is True
    assert result["host"] == state["host"] == "10.77.0.2" or state["host"].startswith("10.77.0.")
    assert known_hosts == f"{state['host']} {backend.host_key}\n"
    assert state["adapter_created"] is True
    assert state["host_address_created"] is True
    operations = [operation for operation, _ in backend.calls]
    assert operations[:3] == ["guest_status", "ssh_network_prepare", "vm_get"]
    assert "guest_put_file" in operations
    assert "guest_exec" in operations


def test_provision_never_asks_for_password_authentication(tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    backend = FakeBackend()
    service = FakeService(backend, settings(tmp_path))
    monkeypatch.setattr(ssh_setup, "_probe", lambda settings_value, vm: {"ok": True, "error": None})
    ssh_setup.provision(service, VM)
    command = [payload for operation, payload in backend.calls if operation == "guest_exec"][0]["command"]
    assert "-Mode Install" in command
    assert "-AllowPassword" not in command
    assert "-HostIp '10.77.0.1'" in command
    assert "-MacAddress '00155D000001'" in command


def test_a_guest_setup_error_leaves_no_state(tmp_path: Path) -> None:
    backend = FakeBackend(guest_output=ERROR_MARKER + "OpenSSH Server is not installed")
    service = FakeService(backend, settings(tmp_path))
    with pytest.raises(BackendError, match="OpenSSH Server is not installed"):
        ssh_setup.provision(service, VM)
    assert not (tmp_path / "ssh" / vm_key(VM) / "state.json").exists()


def test_a_guest_that_never_answers_does_not_stay_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend()
    service = FakeService(backend, settings(tmp_path))
    monkeypatch.setattr(
        ssh_setup, "_probe", lambda settings_value, vm: {"ok": False, "error": "timed out"}
    )
    with pytest.raises(BackendError, match="never answered") as caught:
        ssh_setup.provision(service, VM)
    assert caught.value.code == "ssh_unavailable"
    assert not (tmp_path / "ssh" / vm_key(VM) / "state.json").exists()


def test_an_offline_package_is_uploaded_before_installing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "openssh"
    (package / "sub").mkdir(parents=True)
    (package / "install-sshd.ps1").write_text("# installer\n", encoding="utf-8")
    (package / "sub" / "sshd.exe").write_bytes(b"binary")
    backend = FakeBackend()
    service = FakeService(backend, settings(tmp_path, openssh_package=package))
    monkeypatch.setattr(ssh_setup, "_probe", lambda settings_value, vm: {"ok": True, "error": None})

    ssh_setup.provision(service, VM)

    uploaded = [payload["guest_path"] for operation, payload in backend.calls
                if operation == "guest_put_file"]
    assert r"C:\ProgramData\HyperVConsoleMCP\openssh\install-sshd.ps1" in uploaded
    assert r"C:\ProgramData\HyperVConsoleMCP\openssh\sub\sshd.exe" in uploaded
    command = [payload for operation, payload in backend.calls if operation == "guest_exec"][0]["command"]
    assert "-SshdSource 'C:\\ProgramData\\HyperVConsoleMCP\\openssh'" in command


def test_deprovision_undoes_only_what_provisioning_created(tmp_path: Path) -> None:
    backend = FakeBackend()
    service = FakeService(backend, settings(tmp_path))
    directory = tmp_path / "ssh" / vm_key(VM)
    directory.mkdir(parents=True)
    (directory / "state.json").write_text(json.dumps({
        "vm": VM, "host": "10.77.0.2", "port": 22, "switch_name": "HyperV-MCP-Lab",
        "adapter_name": "HyperV-MCP-Lab", "adapter_created": True,
        "host_ip": "10.77.0.1", "host_address_created": False,
    }))
    (directory / "known_hosts").write_text("10.77.0.2 ssh-ed25519 AAAA\n")

    result = ssh_setup.deprovision(service, VM)

    remove = [payload for operation, payload in backend.calls
              if operation == "ssh_network_remove"][0]
    assert remove["remove_adapter"] is True
    assert remove["remove_host_address"] is False
    assert result["state_removed"] is True
    assert not (directory / "state.json").exists()
    assert not (directory / "known_hosts").exists()
