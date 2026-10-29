"""Host-side SSH provisioning: addresses, keys, state, and the sequence.

The guest is modelled by a fake bridge here; nothing in this file touches a VM.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import BackendError
from hyperv_console_mcp.transport import ssh_setup
from hyperv_console_mcp.transport.psdirect import PowerShellDirectTransport
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
        self.guest_files: dict[str, bytes] = {}

    def invoke(self, operation: str, payload: dict[str, Any], *, timeout=None):
        self.calls.append((operation, dict(payload)))
        if operation == "guest_status":
            return {"vm": payload["vm"], "guest": {"computer_name": "LAB"}}
        if operation == "vm_get":
            # The shape the bridge really returns, so a wrong key here fails the test
            # the way it failed on the live guest.
            return {"vm": {"name": payload["vm"], "network_adapters": [
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
            self.guest_files[payload["guest_path"]] = content
            return {"vm": payload["vm"], "file": {
                "path": payload["guest_path"], "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }}
        if operation == "guest_read_file":
            content = self.guest_files.get(payload["path"])
            if content is None:
                return {"file": {"path": payload["path"], "status": "not_found"}}
            return {"file": {"path": payload["path"], "size": len(content),
                             "sha256": hashlib.sha256(content).hexdigest(), "status": "ok"}}
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


def test_the_mac_address_comes_from_the_shape_vm_get_really_returns(tmp_path: Path) -> None:
    """The adapter lookup reads `network_adapters`, the key the bridge emits.

    Regression: it read `adapters`, found nothing, and reported "the lab adapter
    ... has no MAC address; is the VM running?" on a healthy adapter — after the
    host had already been changed, so a failed provision left the VM moved onto
    the lab switch.
    """
    backend = FakeBackend()
    service = FakeService(backend, settings(tmp_path))

    assert ssh_setup._adapter_mac(service, VM, "HyperV-MCP-Lab") == "00155D000001"

    with pytest.raises(BackendError, match="has no MAC address"):
        ssh_setup._adapter_mac(service, VM, "Some Other Adapter")


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
    # No previous switch was recorded, so nothing is restored.
    assert remove["previous_switch"] == ""
    assert result["state_removed"] is True
    assert not (directory / "state.json").exists()
    assert not (directory / "known_hosts").exists()


@pytest.mark.parametrize("mode", ["status", "deprovision"])
def test_every_guest_mode_ships_the_setup_script_before_running_it(
    tmp_path: Path, mode: str
) -> None:
    """`status` and `deprovision` run the guest script by path, so they ship it first.

    Regression: only `provision` uploaded it. On a guest that had never been
    provisioned, `guest_ssh_status` failed with "the argument
    C:\\ProgramData\\HyperVConsoleMCP\\guest_ssh_setup.ps1 for the -File parameter
    does not exist" instead of reporting that SSH was not set up.
    """
    backend = FakeBackend(guest_output=JSON_MARKER + base64.b64encode(
        json.dumps({"mode": "Status", "sshd_status": "Absent"}).encode()
    ).decode())
    service = FakeService(backend, settings(tmp_path))

    if mode == "status":
        ssh_setup.status(service, VM)
    else:
        ssh_setup.deprovision(service, VM)

    operations = [operation for operation, _ in backend.calls]
    upload = operations.index("guest_put_file")
    execution = operations.index("guest_exec")
    assert upload < execution
    uploaded = backend.calls[upload][1]
    assert uploaded["guest_path"] == ssh_setup.GUEST_SCRIPT
    assert uploaded["overwrite"] is True
    # The script that was shipped is the one the command names.
    assert ssh_setup.GUEST_SCRIPT in backend.calls[execution][1]["command"]


def test_provision_ships_the_operations_script_the_ssh_transport_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every structured `guest_*` call over SSH runs this file by path.

    Regression: provisioning uploaded only the setup script, so the first
    structured operation on a freshly provisioned guest died with the remote
    shell's "not recognized as the name of a cmdlet" — a message that names
    neither the missing file nor the fix.
    """
    backend = FakeBackend()
    service = FakeService(backend, settings(tmp_path))
    monkeypatch.setattr(ssh_setup, "_probe", lambda settings_value, vm: {"ok": True, "error": None})

    ssh_setup.provision(service, VM)

    uploaded = [payload["guest_path"] for operation, payload in backend.calls
                if operation == "guest_put_file"]
    assert ssh_setup.OPERATIONS_SCRIPT in uploaded
    packaged = Path(ssh_setup.__file__).with_name("guest_operations.ps1").read_bytes()
    assert backend.guest_files[ssh_setup.OPERATIONS_SCRIPT] == packaged


def test_status_reports_whether_the_guest_holds_the_operations_script(tmp_path: Path) -> None:
    backend = FakeBackend(guest_output=JSON_MARKER + base64.b64encode(
        json.dumps({"mode": "Status", "sshd_status": "Absent"}).encode()
    ).decode())
    service = FakeService(backend, settings(tmp_path))

    missing = ssh_setup.status(service, VM)
    assert missing["operations_script"]["present"] is False

    ssh_setup._upload_operations(PowerShellDirectTransport(backend, service.settings), VM)
    present = ssh_setup.status(service, VM)
    assert present["operations_script"]["present"] is True
    assert present["operations_script"]["matches"] is True


def test_a_reused_adapter_goes_back_to_the_switch_it_came_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provisioning moves a reused adapter off its switch, so deprovision restores it.

    Regression: `ssh_network_remove` only disconnected the adapter, so a VM that
    arrived on another switch left the lab with no network at all.
    """
    backend = FakeBackend()
    original = backend.invoke

    def prepare_reports_the_old_switch(operation, payload, *, timeout=None):
        result = original(operation, payload, timeout=timeout)
        if operation == "ssh_network_prepare":
            result = {**result, "adapter_created": False, "previous_switch": "Default Switch"}
        return result

    backend.invoke = prepare_reports_the_old_switch  # type: ignore[method-assign]
    service = FakeService(backend, settings(tmp_path))
    monkeypatch.setattr(ssh_setup, "_probe", lambda settings_value, vm: {"ok": True, "error": None})

    ssh_setup.provision(service, VM, use_existing_adapter=True)
    state = json.loads((tmp_path / "ssh" / vm_key(VM) / "state.json").read_text(encoding="utf-8"))
    assert state["previous_switch"] == "Default Switch"

    ssh_setup.deprovision(service, VM)

    remove = [payload for operation, payload in backend.calls
              if operation == "ssh_network_remove"][0]
    assert remove["previous_switch"] == "Default Switch"
    assert remove["remove_adapter"] is False


def _package_zip(tmp_path: Path, *, extra_member: str | None = None) -> Path:
    """A minimal Win32-OpenSSH-shaped archive, built at test time."""
    import zipfile

    archive = tmp_path / "OpenSSH-Win64.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("OpenSSH-Win64/install-sshd.ps1", "# installer\n")
        bundle.writestr("OpenSSH-Win64/sshd.exe", b"binary")
        if extra_member:
            bundle.writestr(extra_member, b"x")
    return archive


def _pin(monkeypatch: pytest.MonkeyPatch, archive: Path) -> None:
    data = archive.read_bytes()
    monkeypatch.setitem(ssh_setup.OPENSSH_RELEASE, "size", len(data))
    monkeypatch.setitem(ssh_setup.OPENSSH_RELEASE, "sha256", hashlib.sha256(data).hexdigest())


def test_the_offline_package_is_verified_against_the_pinned_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _package_zip(tmp_path)
    settings_value = settings(tmp_path)
    destination = ssh_setup.package_directory(settings_value) / ".." / archive.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive.replace(destination)
    _pin(monkeypatch, destination)

    root = ssh_setup.ensure_package(settings_value)

    assert (root / "install-sshd.ps1").is_file()
    assert (root / "sshd.exe").read_bytes() == b"binary"


def test_a_package_that_does_not_match_the_pinned_hash_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _package_zip(tmp_path)
    settings_value = settings(tmp_path)
    destination = ssh_setup.package_directory(settings_value) / ".." / archive.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive.replace(destination)
    _pin(monkeypatch, destination)
    monkeypatch.setitem(ssh_setup.OPENSSH_RELEASE, "sha256", "0" * 64)

    with pytest.raises(BackendError) as caught:
        ssh_setup.ensure_package(settings_value)

    assert caught.value.code == "openssh_package_invalid"
    assert not (ssh_setup.package_directory(settings_value) / "install-sshd.ps1").exists()


def test_an_archive_that_tries_to_escape_its_directory_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _package_zip(tmp_path, extra_member="../../evil.dll")
    settings_value = settings(tmp_path)
    destination = ssh_setup.package_directory(settings_value) / ".." / archive.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive.replace(destination)
    _pin(monkeypatch, destination)

    with pytest.raises(BackendError, match="unsafe path"):
        ssh_setup.ensure_package(settings_value)


def test_a_guest_without_openssh_gets_the_pinned_package_and_a_second_try(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capability is the fast path; a guest with no internet needs the package.

    Regression: the first live guest had no sshd and no route to Windows Update,
    so `Add-WindowsCapability` failed with 0x8024402c and provisioning gave up
    with "set HYPERV_MCP_OPENSSH_PACKAGE" — a manual step the roadmap asked this
    tool to do itself.
    """
    archive = _package_zip(tmp_path)
    packaged = ssh_setup.package_directory(settings(tmp_path)) / ".." / archive.name
    packaged.parent.mkdir(parents=True, exist_ok=True)
    archive.replace(packaged)
    _pin(monkeypatch, packaged)

    backend = FakeBackend()
    original = backend.invoke
    attempts: list[str] = []

    def needs_package_first(operation, payload, *, timeout=None):
        if operation == "guest_exec":
            attempts.append(payload["command"])
            if len(attempts) == 1:
                return {"vm": payload["vm"], "execution": {
                    "success": False, "exit_code": 3,
                    "output": ssh_setup.PACKAGE_MARKER + "Add-WindowsCapability failed 0x8024402c",
                    "had_errors": False, "error_records": [], "output_truncated": False,
                }}
        return original(operation, payload, timeout=timeout)

    backend.invoke = needs_package_first  # type: ignore[method-assign]
    service = FakeService(backend, settings(tmp_path))
    monkeypatch.setattr(ssh_setup, "_probe", lambda settings_value, vm: {"ok": True, "error": None})

    result = ssh_setup.provision(service, VM, use_existing_adapter=True)

    assert result["verified"] is True
    assert len(attempts) == 2
    assert "-SshdSource" not in attempts[0]
    assert f"-SshdSource '{ssh_setup.GUEST_OPENSSH_ROOT}'" in attempts[1]
    uploaded = [payload["guest_path"] for operation, payload in backend.calls
                if operation == "guest_put_file"]
    assert f"{ssh_setup.GUEST_OPENSSH_ROOT}\\install-sshd.ps1" in uploaded


def test_a_result_cut_by_the_output_limit_says_so() -> None:
    """A clipped payload used to surface as a bare base64 error.

    Regression: the guest's JSON line was cut at 65525 characters, and
    guest_ssh_status reported "Invalid base64-encoded string: number of data
    characters (65525) cannot be 1 more than a multiple of 4" — which says
    nothing about the output limit that actually caused it.
    """
    with pytest.raises(BackendError, match="could not be decoded"):
        ssh_setup._marker_result(JSON_MARKER + "A" * 65525)


WINDOWS_POWERSHELL_ONLY = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell") is None,
    reason="needs Windows PowerShell 5.1 to run the guest script",
)


def run_guest_probe(tmp_path: Path, lines: list[str]) -> Any:
    """Dot-source the guest script in a fresh powershell.exe and run ``lines``.

    The script only acts when it is executed, so dot-sourcing loads its helpers
    without touching a guest.
    """
    script = Path(ssh_setup.__file__).with_name("guest_ssh_setup.ps1")
    probe = tmp_path / "probe.ps1"
    probe.write_text(
        "\n".join(["$ErrorActionPreference = 'Stop'", f". '{script}'", *lines]),
        encoding="utf-8-sig",
    )
    completed = subprocess.run(
        [shutil.which("powershell") or "powershell", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-File", str(probe)],
        capture_output=True, text=True, timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@WINDOWS_POWERSHELL_ONLY
def test_the_config_lines_serialize_as_plain_text(tmp_path: Path) -> None:
    """`config_lines` must be text, not provider-decorated objects.

    Regression: Get-Content attaches PSPath/PSDrive/PSProvider to every line and
    ConvertTo-Json walks that whole graph, so the two managed lines of the live
    guest's sshd_config became a 728 KB payload. The output limit cut it in half
    and guest_ssh_status failed with a base64 error instead of reporting state.
    """
    config = tmp_path / "sshd_config"
    config.write_text(
        "# comment\nPubkeyAuthentication yes\nPasswordAuthentication no\nPort 22\n",
        encoding="ascii",
    )

    payload = run_guest_probe(tmp_path, [
        f"$lines = Get-ManagedConfigLine -Path '{config}'",
        "@{ count = @($lines).Count;"
        " json = (@($lines) | ConvertTo-Json -Compress) } | ConvertTo-Json -Compress",
    ])

    assert payload["count"] == 2
    assert json.loads(payload["json"]) == [
        "PubkeyAuthentication yes", "PasswordAuthentication no",
    ]
    assert len(payload["json"]) < 200


@WINDOWS_POWERSHELL_ONLY
def test_the_firewall_scope_is_read_from_the_address_filter(tmp_path: Path) -> None:
    """The rule's own RemoteAddress is empty; the scope lives on the filter.

    Regression: `Get-SetupState` reported `firewall_remote_address: ''` for a
    rule that really was scoped to the lab host, so status could not confirm the
    scope provisioning had just applied.
    """
    payload = run_guest_probe(tmp_path, [
        "$rule = Get-NetFirewallRule -ErrorAction SilentlyContinue |"
        " Where-Object { $_.Direction -eq 'Inbound' } |"
        " Where-Object { @(($_ | Get-NetFirewallAddressFilter).RemoteAddress).Count -gt 0 } |"
        " Select-Object -First 1",
        "if ($null -eq $rule) { '{\"skip\": true}' } else {"
        " @{ expected = ((($rule | Get-NetFirewallAddressFilter).RemoteAddress) -join ',');"
        " actual = (Get-RuleRemoteAddress -Rule $rule) } | ConvertTo-Json -Compress }",
    ])
    if payload.get("skip"):
        pytest.skip("no inbound rule with a remote address on this host")
    assert payload["actual"] == payload["expected"]
    assert payload["actual"]


def test_a_failed_provision_puts_the_host_back(tmp_path: Path) -> None:
    """A guest that refuses must not leave the VM moved onto the lab switch.

    Regression: the network was prepared first and never undone, so a failed
    provision left the guest on the lab switch with no sshd, and the deprovision
    that followed disconnected its only adapter.
    """
    backend = FakeBackend(guest_output=ERROR_MARKER + "install-sshd.ps1 failed")
    service = FakeService(backend, settings(tmp_path))

    with pytest.raises(BackendError, match="install-sshd.ps1 failed"):
        ssh_setup.provision(service, VM, use_existing_adapter=True)

    operations = [operation for operation, _ in backend.calls]
    assert "ssh_network_remove" in operations
    remove = [payload for operation, payload in backend.calls
              if operation == "ssh_network_remove"][0]
    assert remove["remove_adapter"] is True
    assert remove["remove_host_address"] is True
