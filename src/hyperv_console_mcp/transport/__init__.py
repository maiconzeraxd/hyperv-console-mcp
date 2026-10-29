"""Selects the guest transport for a VM from the configured policy."""

from __future__ import annotations

import time
from typing import Any

from .base import ExecResult, FileResult, GuestTransport
from .psdirect import PowerShellDirectTransport
from .ssh import SshTransport

TRANSPORTS = ("auto", "psdirect", "ssh")

# A healthy SSH guest is probed at most this often: `auto` must not pay a
# connection round trip on every tool call.
HEALTH_CACHE_SECONDS = 30.0
_HEALTH: dict[tuple[str, str], tuple[float, bool]] = {}


def _ssh_healthy(transport: SshTransport, vm_name: str) -> bool:
    key = (str(transport.settings.ssh_root), vm_name.casefold())
    now = time.monotonic()
    cached = _HEALTH.get(key)
    if cached is not None and now - cached[0] < HEALTH_CACHE_SECONDS:
        return cached[1]
    healthy = transport.available(vm_name)
    _HEALTH[key] = (now, healthy)
    return healthy


def select_transport(settings: Any, backend: Any, vm_name: str) -> GuestTransport:
    """Return the transport for one VM.

    `auto` prefers SSH only when it was provisioned by this tool and answers
    within the health timeout; otherwise it is PowerShell Direct. An explicit
    `ssh` never falls back silently: an unprovisioned VM is an error.
    """
    choice = settings.guest_transport_for(vm_name)
    ssh = SshTransport(settings)
    if choice == "ssh":
        ssh.state(vm_name)  # Raises PolicyError when provisioning is missing.
        return ssh
    if choice == "auto" and ssh.is_provisioned(vm_name) and _ssh_healthy(ssh, vm_name):
        return ssh
    return PowerShellDirectTransport(backend, settings)


__all__ = [
    "ExecResult", "FileResult", "GuestTransport", "PowerShellDirectTransport",
    "SshTransport", "TRANSPORTS", "select_transport",
]
