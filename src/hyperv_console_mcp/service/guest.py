"""Guest transport selection, command execution, reports and file transfer."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from pathlib import Path
from typing import Any, Callable

from ..errors import BackendError, ValidationError
from ..lab.publish import verify_staged
from ._base import (
    MAX_GUEST_FILE_BYTES,
    ServiceBase,
    T,
    _command_summary,
    _confined_path,
    _integer,
    _text,
)


class GuestService(ServiceBase):
    def _guest_vm(self, vm_name: str) -> str:
        return self._write(vm_name, self.settings.allow_guest, "HYPERV_MCP_ALLOW_GUEST")

    def _transport(self, vm_name: str) -> Any:
        from ..transport import select_transport
        return select_transport(self.settings, self.backend, vm_name)

    def _on_guest(self, name: str, action: Callable[[Any], T]) -> tuple[T, str]:
        """Run one guest operation, repeating it over PowerShell Direct if SSH died.

        `auto` caches an SSH health result for half a minute. A guest that
        reboots in the middle of a lab cycle keeps that cache, so the calls that
        follow are still sent to SSH and fail until it expires. A connection
        failure means the command never reached the guest, which is what makes
        it safe to send it again — and PowerShell Direct does not need the
        network the guest is still bringing up.
        """
        transport = self._transport(name)
        try:
            return action(transport), transport.name
        except BackendError as exc:
            fallback = self._fallback_transport(name, transport, exc)
            if fallback is None:
                raise
            return action(fallback), fallback.name

    def _fallback_transport(self, name: str, transport: Any, exc: BackendError) -> Any | None:
        from ..transport import note_ssh_down
        from ..transport.psdirect import PowerShellDirectTransport
        from ..transport.ssh import SshTransport

        if not isinstance(transport, SshTransport):
            return None
        # A cached session over a guest that has gone away fails at the first
        # channel operation, before the command is sent, but the transport
        # cannot tell that apart from a drop in the middle of one. Both are
        # retried once: the operations `auto` carries are reads, verified
        # transfers, and the operator's own command.
        if exc.code not in {"ssh_unavailable", "ssh_error"}:
            return None
        if self.settings.guest_transport_for(name) != "auto":
            # An explicit `ssh` is a promise to use SSH, not a preference to
            # second-guess; its failures belong to the caller.
            return None
        note_ssh_down(self.settings, name)
        self.audit.record("guest_transport", name, "fallback", exc.code or "ssh_error")
        return PowerShellDirectTransport(self.backend, self.settings)

    def _guest_call(
        self,
        operation: str,
        vm_name: str,
        payload: dict[str, Any] | None = None,
        *,
        summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one structured guest operation through the configured transport."""
        name = self._guest_vm(vm_name)
        request = dict(payload or {})
        used: list[str] = []

        def run() -> dict[str, Any]:
            result, transport_name = self._on_guest(
                name, lambda transport: transport.call(name, operation, request)
            )
            used.append(transport_name)
            return result

        if summary is None:
            result = self._call(operation, name, run)
        else:
            with self._approve(operation, name, summary):
                result = self._call(operation, name, run)
        return {**result, "vm": result.get("vm", name), "transport": used[-1]}

    def guest_exec(
        self, vm_name: str, command: str, *, timeout_seconds: int | None = None
    ) -> dict[str, Any]:
        text = _text(command, "command", 131_072)
        name = self._guest_vm(vm_name)
        timeout = (
            self.settings.timeout_seconds if timeout_seconds is None
            else _integer(timeout_seconds, "timeout_seconds", 10, 3600)
        )
        with self._approve("guest_exec", name, _command_summary(text)):
            execution, _ = self._call(
                "guest_exec",
                name,
                lambda: self._on_guest(
                    name,
                    lambda transport: transport.exec(
                        name,
                        text,
                        timeout=timeout,
                        max_output=self.settings.max_output_chars,
                    ),
                ),
            )
        return {"vm": name, "execution": execution.as_dict()}

    def guest_status(self, vm_name: str) -> dict[str, Any]:
        name = self._guest_vm(vm_name)
        guest, transport = self._call(
            "guest_status", name, lambda: self._on_guest(name, lambda t: t.health(name))
        )
        return {"vm": name, "transport": transport, "guest": guest}

    def guest_processes(self, vm_name: str, name_contains: str = "") -> dict[str, Any]:
        return self._guest_call(
            "guest_processes",
            vm_name,
            {"name_contains": _text(name_contains, "name_contains", 256, allow_empty=True)},
        )

    def guest_services(
        self, vm_name: str, name_contains: str = "", include_stopped: bool = False
    ) -> dict[str, Any]:
        if not isinstance(include_stopped, bool):
            raise ValidationError("include_stopped must be a boolean value.")
        return self._guest_call(
            "guest_services",
            vm_name,
            {
                "name_contains": _text(name_contains, "name_contains", 256, allow_empty=True),
                "include_stopped": include_stopped,
            },
        )

    def guest_events(
        self,
        vm_name: str,
        log_name: str = "System",
        max_events: int = 30,
        event_id: int | None = None,
        level: int | None = None,
        hours: int = 72,
    ) -> dict[str, Any]:
        if log_name not in {"System", "Application", "Security", "Setup"}:
            raise ValidationError("log_name must be System, Application, Security, or Setup.")
        payload: dict[str, Any] = {
            "log_name": log_name,
            "max_events": _integer(max_events, "max_events", 1, 200),
            "hours": _integer(hours, "hours", 1, 24 * 365),
        }
        if event_id is not None:
            payload["event_id"] = _integer(event_id, "event_id", 0, 65_535)
        if level is not None:
            payload["level"] = _integer(level, "level", 1, 5)
        return self._guest_call("guest_events", vm_name, payload)

    def host_crash_events(
        self, vm_name: str, hours: int = 2, max_events: int = 20
    ) -> dict[str, Any]:
        """Hyper-V's own record of a guest crash, read on the host.

        The guest's event 1001 is only written once it has rebooted far enough to
        log it, so it cannot classify a crash that is still writing its dump. The
        host records the crash the moment it happens, with the bugcheck code and
        its parameters as numbers.
        """
        name = self._vm(vm_name)
        return self._run(
            "host_crash_events",
            "host_crash_events",
            {
                "vm": name,
                "hours": _integer(hours, "hours", 1, 24 * 365),
                "max_events": _integer(max_events, "max_events", 1, 200),
            },
            vm_name=name,
        )

    def guest_read_file(
        self, vm_name: str, guest_path: str, max_bytes: int = 65_536
    ) -> dict[str, Any]:
        return self._guest_call(
            "guest_read_file",
            vm_name,
            {
                "path": _text(guest_path, "guest_path", 32_768),
                "max_bytes": _integer(max_bytes, "max_bytes", 1, MAX_GUEST_FILE_BYTES),
            },
        )

    def guest_read_chunk(
        self, vm_name: str, guest_path: str, offset: int, count: int
    ) -> dict[str, Any]:
        return self._guest_call(
            "guest_read_chunk",
            vm_name,
            {
                "path": _text(guest_path, "guest_path", 32_768),
                "offset": _integer(offset, "offset", 0, MAX_GUEST_FILE_BYTES * 256),
                "count": _integer(count, "count", 1, MAX_GUEST_FILE_BYTES),
            },
        )

    def guest_assemble_file(
        self,
        vm_name: str,
        guest_path: str,
        parts_directory: str,
        part_count: int,
        expected_size: int,
        expected_sha256: str,
    ) -> dict[str, Any]:
        digest = _text(expected_sha256, "expected_sha256", 64)
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValidationError("expected_sha256 must be a lowercase SHA-256 hexadecimal string.")
        return self._guest_call(
            "guest_assemble_file",
            vm_name,
            {
                "path": _text(guest_path, "guest_path", 32_768),
                "parts_directory": _text(parts_directory, "parts_directory", 32_768),
                "part_count": _integer(part_count, "part_count", 1, 2048),
                "expected_size": _integer(
                    expected_size, "expected_size", 0, MAX_GUEST_FILE_BYTES * 256
                ),
                "expected_sha256": digest,
            },
        )

    def guest_publish_package(
        self, vm_name: str, package_id: str, transfer_id: str, manifest_sha256: str
    ) -> dict[str, Any]:
        """Verify a staged package on the host, then move it into Packages.

        Two guest calls with the verdict in between: the guest reports the
        manifest bytes and its file listing, ``publish.verify_staged`` decides
        whether that is the package the host approved, and only then is the
        staging directory renamed into place. The window between the two calls
        is one rename's worth of work in a directory only the operator and this
        server write to; the commit refuses an existing package either way.
        """
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}", package_id) is None:
            raise ValidationError("package_id must be 1-64 letters, digits, dots, or hyphens.")
        if re.fullmatch(r"[0-9a-f]{32}", transfer_id) is None:
            raise ValidationError("transfer_id must be a 32-character lowercase hexadecimal id.")
        if re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
            raise ValidationError("manifest_sha256 must be a lowercase SHA-256 hexadecimal string.")
        request = {"package_id": package_id, "transfer_id": transfer_id}
        staged = self._guest_call("guest_package_inspect", vm_name, request)["staged"]
        if not staged.get("manifest_present"):
            raise ValidationError("The staged package has no manifest.")
        if staged.get("exists"):
            raise ValidationError("The package directory already exists; packages are immutable.")
        try:
            manifest = base64.b64decode(str(staged.get("manifest_base64") or ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError("The staged manifest is invalid.") from exc
        verdict = verify_staged(
            package_id=package_id,
            expected_sha256=manifest_sha256,
            manifest=manifest,
            listing=list(staged.get("files") or []),
        )
        committed = self._guest_call("guest_package_commit", vm_name, request)["package"]
        return {
            "vm": committed.get("vm", vm_name),
            "package": {
                "package_id": package_id,
                "guest_path": committed["guest_path"],
                "files_verified": verdict["files_verified"],
                "manifest_sha256": manifest_sha256,
            },
        }

    def guest_write_file(
        self,
        vm_name: str,
        guest_path: str,
        content_base64: str,
        *,
        overwrite: bool = False,
        confirm_path: str | None = None,
        create_parent: bool = False,
    ) -> dict[str, Any]:
        path = _text(guest_path, "guest_path", 32_768)
        _text(content_base64, "content_base64", 14_000_000, allow_empty=True)
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError("content_base64 is not valid base64.") from exc
        if len(content) > MAX_GUEST_FILE_BYTES:
            raise ValidationError("Decoded file content exceeds the 8 MiB limit.")
        if not isinstance(overwrite, bool) or not isinstance(create_parent, bool):
            raise ValidationError("overwrite and create_parent must be boolean values.")
        if overwrite:
            self._confirm(confirm_path or "", path, "confirm_path")
        return self._guest_call(
            "guest_write_file",
            vm_name,
            {
                "path": path,
                "content_base64": content_base64,
                "overwrite": overwrite,
                "create_parent": create_parent,
            },
            summary={
                "path": path, "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(), "overwrite": overwrite,
            },
        )

    def guest_put_file(
        self,
        vm_name: str,
        local_path: str,
        guest_path: str,
        *,
        overwrite: bool = False,
        confirm_path: str | None = None,
    ) -> dict[str, Any]:
        """Copy a host file under HYPERV_MCP_TRANSFER_ROOT into the guest."""
        if not isinstance(overwrite, bool):
            raise ValidationError("overwrite must be a boolean value.")
        source = Path(_confined_path(local_path, "local_path", self.settings.transfer_root, ""))
        if not source.is_file():
            raise ValidationError(f"local_path is not a file: {source}")
        remote = _text(guest_path, "guest_path", 32_768)
        if overwrite:
            self._confirm(confirm_path or "", remote, "confirm_path")
        data = source.read_bytes()
        name = self._guest_vm(vm_name)
        with self._approve(
            "guest_put_file",
            name,
            {
                "local_path": str(source), "guest_path": remote,
                "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "overwrite": overwrite,
            },
        ):
            written, _ = self._call(
                "guest_put_file",
                name,
                lambda: self._on_guest(
                    name,
                    lambda t: t.put_file(name, source, remote, overwrite=overwrite),
                ),
            )
        return {"vm": name, "file": written.as_dict()}

    def guest_get_file(
        self,
        vm_name: str,
        guest_path: str,
        local_path: str,
        *,
        overwrite: bool = False,
        confirm_path: str | None = None,
    ) -> dict[str, Any]:
        """Copy a guest file to a host path under HYPERV_MCP_TRANSFER_ROOT."""
        if not isinstance(overwrite, bool):
            raise ValidationError("overwrite must be a boolean value.")
        remote = _text(guest_path, "guest_path", 32_768)
        target = Path(_confined_path(local_path, "local_path", self.settings.transfer_root, ""))
        if target.exists() and not overwrite:
            raise ValidationError(
                f"local_path already exists: {target}. Pass overwrite=true and confirm_path."
            )
        if overwrite:
            self._confirm(confirm_path or "", str(target), "confirm_path")
        name = self._guest_vm(vm_name)
        with self._approve(
            "guest_get_file", name, {"guest_path": remote, "local_path": str(target)}
        ):
            fetched, _ = self._call(
                "guest_get_file",
                name,
                lambda: self._on_guest(name, lambda t: t.get_file(name, remote, target)),
            )
        return {"vm": name, "file": fetched.as_dict()}
