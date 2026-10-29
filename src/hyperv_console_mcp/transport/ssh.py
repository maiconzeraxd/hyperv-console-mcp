"""SSH transport: one persistent, host-key-pinned connection per VM.

Why paramiko rather than asyncssh: the service layer is synchronous and each
call already runs off the event loop, so a blocking client is the natural fit.
asyncssh would force an event loop into every worker thread for no gain, while
paramiko ships a mature SFTP client and needs no extra runtime.

Nothing here falls back to `ssh.exe`: Win32-OpenSSH has no ControlMaster, so a
persistent session has to be held by the client library.
"""

from __future__ import annotations

import base64
import codecs
import hashlib
import json
import os
import re
import socket
import threading
import uuid
from pathlib import Path
from typing import Any

from .. import credentials
from ..errors import BackendError, PolicyError, ValidationError
from .base import ExecResult, FileResult

# Installed in the guest by guest_ssh_provision; it implements the structured
# guest operations so the two transports run the same PowerShell.
GUEST_SCRIPT = r"C:\ProgramData\HyperVConsoleMCP\guest_operations.ps1"
GUEST_SCRIPT_DIRECTORY = r"C:\ProgramData\HyperVConsoleMCP"
CONNECT_TIMEOUT_SECONDS = 10.0
HEALTH_TIMEOUT_SECONDS = 2.0
KEEPALIVE_SECONDS = 30
# Windows caps a command line at 32 767 characters. `-EncodedCommand` sends the
# script as base64 of UTF-16, which costs about 2.7 characters of command line
# per character of script, so the old always-encoded path failed at roughly 8 KB
# of guest command. Anything longer than this is uploaded and run by path.
MAX_COMMAND_LINE = 28_000
EXEC_MARKER = "HVMCP_EXEC_RESULT:"
JSON_MARKER = "HVMCP_JSON:"
ERROR_MARKER = "HVMCP_ERROR:"

# Operations the installed guest script understands.
GUEST_OPERATIONS = frozenset({
    "guest_processes", "guest_services", "guest_events", "guest_file_info",
    "guest_read_file", "guest_read_chunk", "guest_assemble_file",
    "guest_write_file", "guest_package_inspect", "guest_package_commit",
})

_CLIENTS: dict[tuple[str, int, str, str], Any] = {}
_CLIENTS_LOCK = threading.Lock()


def vm_key(vm_name: str) -> str:
    """A filesystem-safe, case-insensitive directory name for one VM."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", vm_name).casefold() or "vm"


def _paramiko() -> Any:
    try:
        import paramiko
    except ImportError as exc:  # pragma: no cover - exercised with a stub module
        raise PolicyError(
            "The SSH transport needs paramiko. Install it with "
            "python -m pip install 'hyperv-console-mcp[ssh]'."
        ) from exc
    return paramiko


def _encoded_command(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sftp_path(path: str) -> str:
    """OpenSSH on Windows accepts forward slashes and keeps drive letters."""
    return path.replace("\\", "/")


def _local_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


EXEC_WRAPPER = """
$__command = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))
$__output = ''
$__ok = $true
$__code = 0
$__records = @()
$__before = $Error.Count
try {{
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = $null
    $__output = (& ([ScriptBlock]::Create($__command)) 2>&1 | ForEach-Object {{
        if ($_ -is [Management.Automation.ErrorRecord]) {{ $_.ToString() }} else {{ $_ }}
    }} | Out-String -Width 4096)
    if ($null -ne $LASTEXITCODE) {{
        $__code = [int]$LASTEXITCODE
        if ($__code -ne 0) {{ $__ok = $false }}
    }}
}} catch {{
    $__ok = $false
    $__code = 1
    $__output = $__output + [Environment]::NewLine + $_.Exception.Message
}}
$__added = $Error.Count - $__before
if ($__added -gt 0) {{
    $__records = @($Error[0..($__added - 1)] | Where-Object {{
        $_.FullyQualifiedErrorId -notmatch '^NativeCommandError'
    }} | ForEach-Object {{ $_.ToString() }})
}}
$__result = @{{
    success = ($__ok -and $__records.Count -eq 0)
    exit_code = $__code
    output = $__output
    had_errors = ($__records.Count -gt 0)
    error_records = $__records
}}
Write-Output ('{marker}' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(
    ($__result | ConvertTo-Json -Compress -Depth 5))))
"""


class SshTransport:
    """Guest access over SSH with SFTP transfers and a pinned host key."""

    name = "ssh"

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    # -- provisioning state -------------------------------------------------

    def state_directory(self, vm: str) -> Path:
        return Path(self.settings.ssh_root) / vm_key(vm)

    def state(self, vm: str) -> dict[str, Any]:
        path = self.state_directory(vm) / "state.json"
        if not path.is_file():
            raise PolicyError(
                f"SSH is not provisioned for {vm}. Run guest_ssh_provision first."
            )
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PolicyError(f"The SSH state for {vm} is unreadable: {exc}") from exc
        if not isinstance(state, dict) or not str(state.get("host") or "").strip():
            raise PolicyError(f"The SSH state for {vm} has no guest address.")
        return state

    def is_provisioned(self, vm: str) -> bool:
        try:
            self.state(vm)
        except PolicyError:
            return False
        return True

    # -- connection ---------------------------------------------------------

    def _username(self, vm: str) -> str:
        try:
            stored = credentials.CredentialStore().read(vm)
        except PolicyError:
            stored = None
        if stored and stored[0]:
            return stored[0]
        if not self.settings.guest_username:
            raise PolicyError(
                "SSH needs a guest account name. Set one with guest_credential_set or "
                "HYPERV_MCP_GUEST_USERNAME."
            )
        return str(self.settings.guest_username)

    def _key_path(self, vm: str) -> Path:
        return self.state_directory(vm) / "id_ed25519"

    def _known_hosts(self, vm: str) -> Path:
        return self.state_directory(vm) / "known_hosts"

    def _active(self, client: Any) -> bool:
        try:
            transport = client.get_transport()
            return bool(transport is not None and transport.is_active())
        except Exception:
            return False

    def _close_client(self, client: Any) -> None:
        try:
            client.close()
        except Exception:
            pass

    def _client(self, vm: str) -> Any:
        state = self.state(vm)
        paramiko = _paramiko()
        host = str(state["host"])
        port = int(state.get("port") or 22)
        username = self._username(vm)
        key_path = self._key_path(vm)
        cache_key = (host, port, username, str(key_path))
        with _CLIENTS_LOCK:
            client = _CLIENTS.get(cache_key)
            if client is not None and self._active(client):
                return client
            if client is not None:
                self._close_client(client)
                _CLIENTS.pop(cache_key, None)
            client = self._connect(paramiko, vm, host, port, username, key_path)
            _CLIENTS[cache_key] = client
            return client

    def _connect(self, paramiko: Any, vm: str, host: str, port: int,
                 username: str, key_path: Path) -> Any:
        known_hosts = self._known_hosts(vm)
        if not known_hosts.is_file():
            raise PolicyError(
                f"No pinned host key for {vm} at {known_hosts}. Run guest_ssh_provision again."
            )
        if not key_path.is_file():
            raise PolicyError(f"No SSH key for {vm} at {key_path}. Run guest_ssh_provision again.")
        client = paramiko.SSHClient()
        client.load_host_keys(str(known_hosts))
        # Strict verification: an unknown or changed host key is a hard failure,
        # never a trust-on-first-use prompt.
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        key = paramiko.Ed25519Key.from_private_key_file(str(key_path))
        try:
            client.connect(
                hostname=host,
                port=port,
                username=username,
                pkey=key,
                look_for_keys=False,
                allow_agent=False,
                timeout=CONNECT_TIMEOUT_SECONDS,
                banner_timeout=CONNECT_TIMEOUT_SECONDS,
                auth_timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            self._close_client(client)
            raise BackendError(
                f"SSH connection to {vm} at {host}:{port} failed: {exc}",
                code="ssh_unavailable",
            ) from exc
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(KEEPALIVE_SECONDS)
        return client

    def _run(self, vm: str, command: str, *, timeout: float) -> tuple[str, str, int]:
        client = self._client(vm)
        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            try:
                output = stdout.read().decode("utf-8", "replace")
                error = stderr.read().decode("utf-8", "replace")
                status = stdout.channel.recv_exit_status()
            finally:
                stdin.close()
            return output, error, status
        except (OSError, socket.error) as exc:
            self.close(vm)
            raise BackendError(f"SSH session to {vm} failed: {exc}", code="ssh_error") from exc
        except Exception as exc:
            if type(exc).__module__.startswith("paramiko"):
                self.close(vm)
                raise BackendError(f"SSH session to {vm} failed: {exc}", code="ssh_error") from exc
            raise

    @staticmethod
    def encoded_line(script: str) -> str:
        return f"powershell -NoProfile -NonInteractive -EncodedCommand {_encoded_command(script)}"

    def _run_script(
        self, vm: str, script: str, *, timeout: float
    ) -> tuple[str, str, int]:
        """Run a PowerShell script in the guest, however long it is.

        The command line is the wrong place for a big script: Windows caps it at
        32 767 characters and the encoding nearly triples the length. A script
        that would not fit is written to a temporary file in the guest and run
        with `-File`, which has no such limit.
        """
        line = self.encoded_line(script)
        if len(line) <= MAX_COMMAND_LINE:
            return self._run(vm, line, timeout=timeout)
        return self._run_uploaded(vm, script, timeout=timeout)

    def _run_uploaded(
        self, vm: str, script: str, *, timeout: float
    ) -> tuple[str, str, int]:
        remote = f"{GUEST_SCRIPT_DIRECTORY}\\exec-{uuid.uuid4().hex}.ps1"
        client = self._client(vm)
        sftp = client.open_sftp()
        try:
            # Windows PowerShell 5.1 only reads a script as UTF-8 when it starts
            # with a BOM; without one it falls back to the ANSI code page.
            with sftp.open(_sftp_path(remote), "wb") as handle:
                handle.write(codecs.BOM_UTF8 + script.encode("utf-8"))
        finally:
            sftp.close()
        try:
            return self._run(
                vm,
                "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass "
                f"-File {ps_quote(remote)}",
                timeout=timeout,
            )
        finally:
            try:
                cleanup = client.open_sftp()
                try:
                    cleanup.remove(_sftp_path(remote))
                finally:
                    cleanup.close()
            except Exception:
                # A leftover script is harmless; losing the real result is not.
                pass

    # -- GuestTransport -----------------------------------------------------

    def exec(self, vm: str, command: str, *, timeout: float, max_output: int) -> ExecResult:
        script = EXEC_WRAPPER.format(
            payload=base64.b64encode(command.encode("utf-8")).decode("ascii"),
            marker=EXEC_MARKER,
        )
        output, error, status = self._run_script(vm, script, timeout=timeout)
        payload = None
        for line in output.splitlines():
            if line.startswith(EXEC_MARKER):
                try:
                    payload = json.loads(base64.b64decode(line[len(EXEC_MARKER):]).decode("utf-8"))
                except (ValueError, TypeError):
                    payload = None
        if not isinstance(payload, dict):
            # The wrapper itself did not run: report the raw channel result.
            text = (output + error).strip()
            return ExecResult(
                success=status == 0, exit_code=int(status), output=text[:max_output],
                had_errors=status != 0, error_records=(error.strip(),) if error.strip() else (),
                truncated=len(text) > max_output, transport=self.name,
            )
        text = str(payload.get("output") or "")
        return ExecResult(
            success=bool(payload.get("success")),
            exit_code=int(payload.get("exit_code") or 0),
            output=text[:max_output],
            had_errors=bool(payload.get("had_errors")),
            error_records=tuple(str(item) for item in payload.get("error_records") or ()),
            truncated=len(text) > max_output,
            transport=self.name,
        )

    def health(self, vm: str) -> dict[str, Any]:
        command = (
            "$os = Get-CimInstance -ClassName 'Win32_OperatingSystem'; "
            "[pscustomobject]@{ computer_name = $env:COMPUTERNAME; "
            "windows_version = [string]$os.Version } | ConvertTo-Json -Compress"
        )
        # The guest's sshd shell is powershell.exe, so a plain `-Command` string
        # is parsed twice and `$os` is expanded by the outer shell before the
        # inner one sees it. Encoding the command keeps it intact.
        output, error, status = self._run_script(
            vm, command, timeout=HEALTH_TIMEOUT_SECONDS
        )
        if status != 0:
            raise BackendError(
                f"The SSH guest agent in {vm} did not answer: {error.strip() or output.strip()}",
                code="ssh_unavailable",
            )
        try:
            parsed = json.loads(output.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise BackendError(f"The SSH guest in {vm} returned no health data.", code="ssh_error") from exc
        return {**parsed, "transport": self.name}

    def available(self, vm: str) -> bool:
        """A non-raising probe, used only by the `auto` selection."""
        try:
            self.health(vm)
        except Exception:
            return False
        return True

    def call(self, vm: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one structured guest operation through the installed guest script."""
        if operation not in GUEST_OPERATIONS:
            raise ValidationError(f"{operation!r} is not a guest operation.")
        request = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        # A large payload (a file being written, for instance) makes this line
        # far longer than Windows allows, so it goes through the same runner as
        # every other script rather than straight onto the command line.
        script = (
            f"& {ps_quote(GUEST_SCRIPT)} -Operation {ps_quote(operation)} "
            f"-Request {ps_quote(request)}"
        )
        output, error, status = self._run_script(
            vm, script, timeout=self.settings.timeout_seconds
        )
        for line in output.splitlines():
            if line.startswith(JSON_MARKER):
                result = json.loads(base64.b64decode(line[len(JSON_MARKER):]).decode("utf-8"))
                if not isinstance(result, dict):
                    raise BackendError(f"The guest script returned no object for {operation}.",
                                       code="ssh_error")
                # Windows PowerShell 5.1 collapses a one-item array to a scalar.
                for key in ("processes", "services", "events"):
                    value = result.get(key)
                    if key in result and not isinstance(value, list):
                        result[key] = [] if value is None else [value]
                result.setdefault("vm", vm)
                return result
            if line.startswith(ERROR_MARKER):
                raise BackendError(
                    f"The guest script rejected {operation}: {line[len(ERROR_MARKER):]}",
                    code="guest_error",
                )
        raise BackendError(
            f"The guest script for {operation} produced no result: "
            f"{(error.strip() or output.strip())[:400]}",
            code="ssh_error",
        )

    def file_info(self, vm: str, remote: str) -> dict[str, Any]:
        return self.call(vm, "guest_file_info", {"path": remote})["file"]

    def _guest_sha256(self, vm: str, remote: str) -> str:
        command = (
            f"if (-not (Test-Path -LiteralPath {ps_quote(remote)} -PathType Leaf)) {{ "
            "'HVMCP_HASH:not_found' } else { 'HVMCP_HASH:' + "
            f"(Get-FileHash -LiteralPath {ps_quote(remote)} -Algorithm SHA256).Hash.ToLowerInvariant() }}"
        )
        output, _, _ = self._run_script(
            vm, command, timeout=self.settings.timeout_seconds
        )
        for line in output.splitlines():
            if line.startswith("HVMCP_HASH:"):
                value = line[len("HVMCP_HASH:"):].strip()
                if value == "not_found":
                    raise ValidationError(f"Guest file not found: {remote}")
                return value
        raise BackendError(f"Could not hash {remote} in {vm}.", code="ssh_error")

    def put_file(self, vm: str, local: Path, remote: str, *, overwrite: bool) -> FileResult:
        source = Path(local)
        if not source.is_file():
            raise ValidationError(f"{source} is not a file.")
        client = self._client(vm)
        sftp = client.open_sftp()
        temporary = _sftp_path(remote + ".part")
        target = _sftp_path(remote)
        try:
            exists = True
            try:
                sftp.stat(target)
            except IOError:
                exists = False
            if exists and not overwrite:
                raise ValidationError(
                    f"Guest file already exists; overwrite was not approved: {remote}"
                )
            try:
                sftp.put(str(source), temporary)
                if exists:
                    sftp.remove(target)
                sftp.rename(temporary, target)
            except Exception:
                try:
                    sftp.remove(temporary)
                except IOError:
                    pass
                raise
        finally:
            sftp.close()
        expected = _local_sha256(source)
        actual = self._guest_sha256(vm, remote)
        if actual != expected:
            # Never leave a file the host cannot vouch for in the guest.
            try:
                sftp = client.open_sftp()
                try:
                    sftp.remove(target)
                finally:
                    sftp.close()
            except Exception:
                pass
            raise BackendError(
                f"{remote} received different bytes ({actual} != {expected}).",
                code="transfer_mismatch",
            )
        return FileResult(
            path=remote, size=source.stat().st_size, sha256=actual,
            transport=self.name, bytes_transferred=source.stat().st_size,
        )

    def get_file(self, vm: str, remote: str, local: Path) -> FileResult:
        target = Path(local)
        target.parent.mkdir(parents=True, exist_ok=True)
        expected = self._guest_sha256(vm, remote)
        client = self._client(vm)
        sftp = client.open_sftp()
        temporary = str(target) + ".part"
        try:
            sftp.get(_sftp_path(remote), temporary)
        finally:
            sftp.close()
        try:
            actual = _local_sha256(Path(temporary))
            if actual != expected:
                raise BackendError(
                    f"{remote} arrived with different bytes ({actual} != {expected}).",
                    code="transfer_mismatch",
                )
            os.replace(temporary, target)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise
        return FileResult(
            path=str(target), size=target.stat().st_size, sha256=actual,
            transport=self.name, bytes_transferred=target.stat().st_size,
        )

    def close(self, vm: str | None = None) -> None:
        host = str(self.state(vm)["host"]) if vm is not None else None
        with _CLIENTS_LOCK:
            keys = [key for key in _CLIENTS if host is None or key[0] == host]
            for key in keys:
                self._close_client(_CLIENTS.pop(key))


__all__ = ["GUEST_OPERATIONS", "GUEST_SCRIPT", "SshTransport", "ps_quote", "vm_key"]
