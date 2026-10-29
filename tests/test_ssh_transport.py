"""SSH transport: connection policy, exec envelope, and verified transfers.

paramiko is replaced by a stub module here; nothing in this file touches a real
SSH server. The guest script itself is exercised live only.
"""

from __future__ import annotations

import base64
import codecs
import hashlib
import json
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import BackendError, PolicyError, ValidationError
from hyperv_console_mcp.transport import _HEALTH, select_transport
from hyperv_console_mcp.transport import ssh as ssh_module
from hyperv_console_mcp.transport.psdirect import PowerShellDirectTransport
from hyperv_console_mcp.transport.ssh import (
    EXEC_MARKER,
    JSON_MARKER,
    MAX_COMMAND_LINE,
    SshTransport,
    vm_key,
)

VM = "LAB-Test"


def settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "allowed_vms": frozenset({VM}),
        "allow_guest": True,
        "guest_username": "mcn1",
        "guest_transport": "ssh",
        "ssh_root": tmp_path / "ssh",
        "transfer_root": tmp_path / "transfers",
    }
    values.update(overrides)
    return Settings(**values)


def provision(tmp_path: Path, host: str = "10.77.0.2", vm: str = VM) -> Path:
    directory = tmp_path / "ssh" / vm_key(vm)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "state.json").write_text(json.dumps({"vm": vm, "host": host, "port": 22}))
    (directory / "known_hosts").write_text(f"{host} ssh-ed25519 AAAAfake\n")
    (directory / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    return directory


def _norm(path: str) -> str:
    return path.replace("\\", "/").casefold()


def _default_exec(command: str) -> dict[str, Any]:
    return {"success": True, "exit_code": 0, "output": "ok\r\n",
            "had_errors": False, "error_records": []}


def _default_guest(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True}


class FakeChannel:
    def __init__(self, status: int = 0) -> None:
        self._status = status

    def recv_exit_status(self) -> int:
        return self._status


class FakeStream:
    def __init__(self, data: str = "", status: int = 0) -> None:
        self._data = data.encode("utf-8")
        self.channel = FakeChannel(status)

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        pass


class FakeTransportChannel:
    def __init__(self) -> None:
        self.keepalive: int | None = None
        self.active = True

    def is_active(self) -> bool:
        return self.active

    def set_keepalive(self, seconds: int) -> None:
        self.keepalive = seconds


class FakeSftpFile:
    """A write-only handle, which is all the uploaded-script path needs."""

    def __init__(self, sftp: "FakeSftp", path: str) -> None:
        self.sftp = sftp
        self.path = path
        self.buffer = b""

    def write(self, data: bytes) -> int:
        self.buffer += data
        return len(data)

    def close(self) -> None:
        self.sftp.client.files[_norm(self.path)] = self.buffer

    def __enter__(self) -> "FakeSftpFile":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class FakeSftp:
    def __init__(self, client: "FakeSSHClient") -> None:
        self.client = client

    def open(self, path: str, mode: str = "rb") -> FakeSftpFile:
        if "w" not in mode:
            raise IOError("this stub only writes")
        return FakeSftpFile(self, path)

    def stat(self, path: str) -> object:
        if _norm(path) not in self.client.files:
            raise IOError("No such file")
        return object()

    def put(self, local: str, remote: str) -> None:
        data = Path(local).read_bytes()
        if self.client.corrupt_upload:
            data = data[::-1]
        self.client.files[_norm(remote)] = data

    def get(self, remote: str, local: str) -> None:
        data = self.client.files[_norm(remote)]
        if self.client.corrupt_download:
            data = data[:-1]
        Path(local).write_bytes(data)

    def remove(self, path: str) -> None:
        self.client.files.pop(_norm(path), None)

    def rename(self, source: str, target: str) -> None:
        self.client.files[_norm(target)] = self.client.files.pop(_norm(source))

    def close(self) -> None:
        pass


class FakeSSHClient:
    """A stub SSHClient; tests configure behaviour on the class."""

    instances: list["FakeSSHClient"] = []
    reject_host_key = False
    health_fails = False
    corrupt_upload = False
    corrupt_download = False
    preload_files: dict[str, bytes] = {}
    raw_output: tuple[str, int] | None = None
    reject_file_run = False
    exec_handler = _default_exec
    guest_handler = _default_guest

    def __init__(self) -> None:
        self.loaded_host_keys: list[str] = []
        self.policy: Any = None
        self.connect_kwargs: dict[str, Any] = {}
        self.commands: list[str] = []
        self.scripts: list[str] = []
        self.files: dict[str, bytes] = dict(FakeSSHClient.preload_files)
        self.exec_handler = FakeSSHClient.exec_handler
        self.guest_handler = FakeSSHClient.guest_handler
        self.corrupt_upload = FakeSSHClient.corrupt_upload
        self.corrupt_download = FakeSSHClient.corrupt_download
        self.transport = FakeTransportChannel()
        FakeSSHClient.instances.append(self)

    def load_host_keys(self, path: str) -> None:
        self.loaded_host_keys.append(path)

    def set_missing_host_key_policy(self, policy: Any) -> None:
        self.policy = policy

    def connect(self, **kwargs: Any) -> None:
        if FakeSSHClient.reject_host_key:
            raise FakeSSHException("Server host key not found in known_hosts")
        self.connect_kwargs = kwargs

    def get_transport(self) -> FakeTransportChannel:
        return self.transport

    def _hash_line(self, script: str) -> str:
        match = re.search(r"-LiteralPath '((?:[^']|'')*)'", script)
        path = match.group(1).replace("''", "'")
        data = self.files.get(_norm(path))
        if data is None:
            return "HVMCP_HASH:not_found"
        return "HVMCP_HASH:" + hashlib.sha256(data).hexdigest()

    def exec_command(self, command: str, timeout: float | None = None):
        self.commands.append(command)
        if FakeSSHClient.raw_output is not None:
            text, status = FakeSSHClient.raw_output
            return FakeStream(), FakeStream(text, status), FakeStream()
        if "-EncodedCommand " in command:
            script = base64.b64decode(command.rsplit(" ", 1)[1]).decode("utf-16-le")
            return self._respond(script)
        uploaded = re.search(r"-File '((?:[^']|'')*)'", command)
        if uploaded is not None:
            if FakeSSHClient.reject_file_run:
                raise FakeSSHException("Channel closed")
            path = uploaded.group(1).replace("''", "'")
            data = self.files.get(_norm(path))
            if data is None:
                return FakeStream(), FakeStream("", 1), FakeStream()
            # PowerShell 5.1 reads a script as UTF-8 only when it starts with a BOM.
            assert data.startswith(codecs.BOM_UTF8)
            return self._respond(data[len(codecs.BOM_UTF8):].decode("utf-8"))
        return self._respond(command)

    def _respond(self, script: str):
        self.scripts.append(script)
        wrapped = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script)
        if wrapped is not None:
            user_command = base64.b64decode(wrapped.group(1)).decode("utf-8")
            line = EXEC_MARKER + base64.b64encode(
                json.dumps(self.exec_handler(user_command)).encode()
            ).decode()
            return FakeStream(), FakeStream(line), FakeStream()
        if "guest_operations.ps1" in script:
            operation = re.search(r"-Operation '([^']*)'", script).group(1)
            request = re.search(r"-Request '([^']*)'", script).group(1)
            payload = json.loads(base64.b64decode(request).decode("utf-8"))
            line = JSON_MARKER + base64.b64encode(
                json.dumps(self.guest_handler(operation, payload)).encode()
            ).decode()
            return FakeStream(), FakeStream(line), FakeStream()
        if "Get-FileHash" in script:
            return FakeStream(), FakeStream(self._hash_line(script)), FakeStream()
        # The health probe: a plain script, not the exec wrapper.
        if FakeSSHClient.health_fails:
            return FakeStream(), FakeStream("", 1), FakeStream()
        body = json.dumps({"computer_name": "LAB", "windows_version": "10.0"})
        return FakeStream(), FakeStream(body), FakeStream()

    def open_sftp(self) -> FakeSftp:
        return FakeSftp(self)

    def close(self) -> None:
        self.transport.active = False


class FakeSSHException(Exception):
    pass


FakeSSHException.__module__ = "paramiko"


class FakeRejectPolicy:
    pass


class FakeKey:
    @staticmethod
    def from_private_key_file(path: str) -> str:
        return f"key:{path}"


@pytest.fixture(autouse=True)
def fake_paramiko(monkeypatch: pytest.MonkeyPatch):
    module = types.ModuleType("paramiko")
    module.SSHClient = FakeSSHClient
    module.RejectPolicy = FakeRejectPolicy
    module.Ed25519Key = FakeKey
    module.SSHException = FakeSSHException
    monkeypatch.setitem(sys.modules, "paramiko", module)
    FakeSSHClient.instances.clear()
    FakeSSHClient.reject_host_key = False
    FakeSSHClient.health_fails = False
    FakeSSHClient.corrupt_upload = False
    FakeSSHClient.corrupt_download = False
    FakeSSHClient.preload_files = {}
    FakeSSHClient.raw_output = None
    FakeSSHClient.reject_file_run = False
    FakeSSHClient.exec_handler = _default_exec
    FakeSSHClient.guest_handler = _default_guest
    ssh_module._CLIENTS.clear()
    _HEALTH.clear()
    yield module
    ssh_module._CLIENTS.clear()


def test_an_unprovisioned_vm_is_a_policy_error_not_a_silent_fallback(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="guest_ssh_provision"):
        select_transport(settings(tmp_path), None, VM)


def test_explicit_ssh_requires_the_state_file(tmp_path: Path) -> None:
    provision(tmp_path)
    (tmp_path / "ssh" / vm_key(VM) / "state.json").unlink()
    with pytest.raises(PolicyError, match="not provisioned"):
        select_transport(settings(tmp_path), None, VM)


def test_auto_uses_ssh_only_when_the_guest_answers(tmp_path: Path) -> None:
    provision(tmp_path)
    transport = select_transport(settings(tmp_path, guest_transport="auto"), None, VM)
    assert transport.name == "ssh"

    _HEALTH.clear()
    FakeSSHClient.health_fails = True
    fallback = select_transport(settings(tmp_path, guest_transport="auto"), None, VM)
    assert isinstance(fallback, PowerShellDirectTransport)


def test_auto_without_provisioning_is_powershell_direct(tmp_path: Path) -> None:
    transport = select_transport(settings(tmp_path, guest_transport="auto"), None, VM)
    assert isinstance(transport, PowerShellDirectTransport)


def test_the_connection_pins_the_host_key_and_never_uses_an_agent(tmp_path: Path) -> None:
    provision(tmp_path)
    SshTransport(settings(tmp_path)).health(VM)
    client = FakeSSHClient.instances[-1]

    directory = tmp_path / "ssh" / vm_key(VM)
    assert client.loaded_host_keys == [str(directory / "known_hosts")]
    assert isinstance(client.policy, FakeRejectPolicy)
    assert client.connect_kwargs["hostname"] == "10.77.0.2"
    assert client.connect_kwargs["username"] == "mcn1"
    assert client.connect_kwargs["look_for_keys"] is False
    assert client.connect_kwargs["allow_agent"] is False
    assert client.connect_kwargs["pkey"] == f"key:{directory / 'id_ed25519'}"
    assert client.transport.keepalive == 30


def test_the_health_probe_survives_the_guest_shell(tmp_path: Path) -> None:
    """The guest's sshd shell is powershell.exe, so a `-Command` string is parsed twice.

    Regression: health was sent as `powershell -Command "$os = Get-CimInstance ..."`,
    the outer shell expanded `$os` to nothing before the inner one ran, and
    guest_status reported `windows_version: null` on the live guest while the same
    probe over PowerShell Direct returned the version.
    """
    provision(tmp_path)

    result = SshTransport(settings(tmp_path)).health(VM)

    client = FakeSSHClient.instances[-1]
    assert "-EncodedCommand" in client.commands[-1]
    script = client.scripts[-1]
    assert "$os = Get-CimInstance" in script
    assert "windows_version = [string]$os.Version" in script
    assert result["windows_version"] == "10.0"


def test_a_rejected_host_key_is_reported_not_trusted(tmp_path: Path) -> None:
    provision(tmp_path)
    FakeSSHClient.reject_host_key = True
    with pytest.raises(BackendError, match="host key") as caught:
        SshTransport(settings(tmp_path)).health(VM)
    assert caught.value.code == "ssh_unavailable"


def test_one_client_is_reused_for_the_same_vm(tmp_path: Path) -> None:
    provision(tmp_path)
    transport = SshTransport(settings(tmp_path))
    transport.health(VM)
    transport.health(VM)
    assert len(FakeSSHClient.instances) == 1


def test_exec_round_trips_the_command_and_reports_errors(tmp_path: Path) -> None:
    provision(tmp_path)
    seen: list[str] = []

    def handler(command: str) -> dict[str, Any]:
        seen.append(command)
        return {"success": False, "exit_code": 3, "output": "boom\r\n",
                "had_errors": True, "error_records": ["Get-Item: not found"]}

    FakeSSHClient.exec_handler = handler
    result = SshTransport(settings(tmp_path)).exec(
        VM, "Get-Item 'C:\\nope' | Out-String", timeout=30, max_output=4096
    )
    assert seen == ["Get-Item 'C:\\nope' | Out-String"]
    assert result.transport == "ssh"
    assert result.success is False
    assert result.exit_code == 3
    assert result.had_errors is True
    assert result.error_records == ("Get-Item: not found",)


def test_exec_truncates_the_output(tmp_path: Path) -> None:
    provision(tmp_path)
    FakeSSHClient.exec_handler = lambda command: {
        "success": True, "exit_code": 0, "output": "x" * 500,
        "had_errors": False, "error_records": [],
    }
    result = SshTransport(settings(tmp_path)).exec(VM, "Write-Output x", timeout=30, max_output=10)
    assert result.output == "x" * 10
    assert result.truncated is True


def test_exec_without_the_wrapper_reports_the_channel_result(tmp_path: Path) -> None:
    provision(tmp_path)
    FakeSSHClient.raw_output = ("raw failure\r\n", 2)
    result = SshTransport(settings(tmp_path)).exec(VM, "exit 2", timeout=30, max_output=4096)
    assert result.success is False
    assert result.exit_code == 2
    assert "raw failure" in result.output


def test_a_short_script_stays_on_the_command_line(tmp_path: Path) -> None:
    provision(tmp_path)
    SshTransport(settings(tmp_path)).exec(VM, "Get-Date", timeout=30, max_output=1024)
    client = FakeSSHClient.instances[-1]
    assert "-EncodedCommand" in client.commands[-1]
    assert len(client.commands[-1]) <= MAX_COMMAND_LINE


def test_a_hundred_kilobyte_command_is_uploaded_instead_of_encoded(
    tmp_path: Path,
) -> None:
    """Windows caps a command line at 32 767 characters, and the encoding triples it.

    Regression: every script went out as `-EncodedCommand`, so `guest_exec` failed
    near 8 KB of command and `guest_write_file` near 18 KB, against the 131 072
    bytes the API accepts. A script too long for the line is now written to the
    guest over SFTP and run with `-File`.
    """
    provision(tmp_path)
    seen: list[str] = []

    def handler(command: str) -> dict[str, Any]:
        seen.append(command)
        return _default_exec(command)

    FakeSSHClient.exec_handler = handler
    command = "Write-Output " + "A" * 100_000
    result = SshTransport(settings(tmp_path)).exec(VM, command, timeout=60, max_output=1024)

    client = FakeSSHClient.instances[-1]
    assert seen == [command]
    assert result.success is True
    # The line that would have been generated is over the cap; nothing that long
    # was ever sent.
    assert len(SshTransport.encoded_line(client.scripts[-1])) > MAX_COMMAND_LINE
    assert all(len(line) <= MAX_COMMAND_LINE for line in client.commands)
    assert all("-EncodedCommand" not in line for line in client.commands)
    assert any("-File " in line for line in client.commands)
    # And the uploaded script is cleaned up afterwards.
    assert [path for path in client.files if "exec-" in path] == []


def test_a_large_guest_call_payload_also_goes_through_the_upload_path(
    tmp_path: Path,
) -> None:
    provision(tmp_path)
    seen: list[tuple[str, dict[str, Any]]] = []
    FakeSSHClient.guest_handler = lambda operation, payload: (
        seen.append((operation, payload)) or {"written": True}
    )
    content = base64.b64encode(b"x" * 100_000).decode("ascii")

    result = SshTransport(settings(tmp_path)).call(
        VM, "guest_write_file", {"path": r"C:\DriverLab\big.bin", "content_base64": content}
    )

    assert seen and seen[0][0] == "guest_write_file"
    assert seen[0][1]["content_base64"] == content
    assert result["written"] is True
    client = FakeSSHClient.instances[-1]
    assert all(len(line) <= MAX_COMMAND_LINE for line in client.commands)
    assert [path for path in client.files if "exec-" in path] == []


def test_the_uploaded_script_is_removed_when_the_run_fails(tmp_path: Path) -> None:
    provision(tmp_path)
    FakeSSHClient.reject_file_run = True
    with pytest.raises(BackendError):
        SshTransport(settings(tmp_path)).exec(VM, "Write-Output " + "A" * 100_000,
                                              timeout=60, max_output=1024)
    client = FakeSSHClient.instances[-1]
    assert [path for path in client.files if "exec-" in path] == []


def test_put_file_verifies_the_digest_the_guest_reports(tmp_path: Path) -> None:
    provision(tmp_path)
    source = tmp_path / "driver.sys"
    source.write_bytes(b"driver-bytes")
    result = SshTransport(settings(tmp_path)).put_file(
        VM, source, r"C:\DriverLab\Packages\p\driver.sys", overwrite=False
    )
    client = FakeSSHClient.instances[-1]
    assert result.transport == "ssh"
    assert result.sha256 == hashlib.sha256(b"driver-bytes").hexdigest()
    assert client.files[_norm(r"C:\DriverLab\Packages\p\driver.sys")] == b"driver-bytes"
    assert _norm(r"C:\DriverLab\Packages\p\driver.sys.part") not in client.files


def test_put_file_refuses_to_replace_without_overwrite(tmp_path: Path) -> None:
    provision(tmp_path)
    FakeSSHClient.preload_files = {_norm(r"C:\DriverLab\keep.sys"): b"old"}
    source = tmp_path / "new.sys"
    source.write_bytes(b"new")
    with pytest.raises(ValidationError, match="overwrite"):
        SshTransport(settings(tmp_path)).put_file(
            VM, source, r"C:\DriverLab\keep.sys", overwrite=False
        )
    assert FakeSSHClient.instances[-1].files[_norm(r"C:\DriverLab\keep.sys")] == b"old"


def test_put_file_rejects_bytes_that_arrived_different(tmp_path: Path) -> None:
    provision(tmp_path)
    FakeSSHClient.corrupt_upload = True
    source = tmp_path / "driver.sys"
    source.write_bytes(b"driver-bytes")
    with pytest.raises(BackendError) as caught:
        SshTransport(settings(tmp_path)).put_file(VM, source, r"C:\DriverLab\p.sys", overwrite=True)
    assert caught.value.code == "transfer_mismatch"


def test_get_file_writes_the_target_only_after_verifying(tmp_path: Path) -> None:
    provision(tmp_path)
    FakeSSHClient.preload_files = {_norm(r"C:\DriverLab\dump.dmp"): b"dump-bytes"}
    target = tmp_path / "host" / "dump.dmp"
    result = SshTransport(settings(tmp_path)).get_file(VM, r"C:\DriverLab\dump.dmp", target)
    assert target.read_bytes() == b"dump-bytes"
    assert result.sha256 == hashlib.sha256(b"dump-bytes").hexdigest()
    assert not Path(str(target) + ".part").exists()


def test_get_file_leaves_nothing_behind_when_the_bytes_differ(tmp_path: Path) -> None:
    provision(tmp_path)
    FakeSSHClient.preload_files = {_norm(r"C:\DriverLab\dump.dmp"): b"dump-bytes"}
    FakeSSHClient.corrupt_download = True
    target = tmp_path / "host" / "dump.dmp"
    with pytest.raises(BackendError, match="different bytes"):
        SshTransport(settings(tmp_path)).get_file(VM, r"C:\DriverLab\dump.dmp", target)
    assert not target.exists()
    assert not Path(str(target) + ".part").exists()


def test_a_missing_guest_file_is_a_validation_error(tmp_path: Path) -> None:
    provision(tmp_path)
    with pytest.raises(ValidationError, match="not found"):
        SshTransport(settings(tmp_path)).get_file(VM, r"C:\DriverLab\missing.dmp", tmp_path / "x")


def test_structured_operations_run_the_installed_guest_script(tmp_path: Path) -> None:
    provision(tmp_path)
    seen: list[tuple[str, dict[str, Any]]] = []

    def handler(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        seen.append((operation, payload))
        return {"processes": [{"name": "lsass"}]}

    FakeSSHClient.guest_handler = handler
    result = SshTransport(settings(tmp_path)).call(VM, "guest_processes", {"name_contains": "ls"})
    assert seen == [("guest_processes", {"name_contains": "ls"})]
    assert result["vm"] == VM
    assert result["processes"] == [{"name": "lsass"}]


def test_a_one_item_list_from_powershell_stays_a_list(tmp_path: Path) -> None:
    provision(tmp_path)
    FakeSSHClient.guest_handler = lambda operation, payload: {"services": {"name": "Spooler"}}
    result = SshTransport(settings(tmp_path)).call(VM, "guest_services", {})
    assert result["services"] == [{"name": "Spooler"}]


def test_an_unsupported_operation_is_rejected_before_connecting(tmp_path: Path) -> None:
    provision(tmp_path)
    with pytest.raises(ValidationError, match="not a guest operation"):
        SshTransport(settings(tmp_path)).call(VM, "vm_power", {})
    assert FakeSSHClient.instances == []


def test_missing_paramiko_is_a_policy_error_with_the_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provision(tmp_path)
    monkeypatch.setitem(sys.modules, "paramiko", None)
    with pytest.raises(PolicyError, match=r"\[ssh\]"):
        SshTransport(settings(tmp_path)).health(VM)


def test_the_state_directory_is_filesystem_safe() -> None:
    assert vm_key("LAB-Test") == "lab-test"
    assert vm_key("lab/../x") == "lab_.._x"
