"""Selects the guest transport for a VM from the configured policy."""

from __future__ import annotations

import time
from typing import Any

from .base import ExecResult, FileResult, GuestTransport
from .choice import TransportChoiceStore
from .psdirect import PowerShellDirectTransport
from .ssh import SshTransport

TRANSPORTS = ("auto", "psdirect", "ssh")

# A healthy SSH guest is probed at most this often: `auto` must not pay a
# connection round trip on every tool call.
HEALTH_CACHE_SECONDS = 30.0
_HEALTH: dict[tuple[str, str], tuple[float, bool]] = {}


def _health_key(settings: Any, vm_name: str) -> tuple[str, str]:
    return (str(settings.ssh_root), vm_name.casefold())


def _ssh_healthy(transport: SshTransport, vm_name: str) -> bool:
    key = _health_key(transport.settings, vm_name)
    now = time.monotonic()
    cached = _HEALTH.get(key)
    if cached is not None and now - cached[0] < HEALTH_CACHE_SECONDS:
        return cached[1]
    healthy = transport.available(vm_name)
    _HEALTH[key] = (now, healthy)
    return healthy


def invalidate_health(settings: Any, vm_name: str) -> None:
    """Forget the cached SSH health result for one VM."""
    _HEALTH.pop(_health_key(settings, vm_name), None)


def note_ssh_down(settings: Any, vm_name: str) -> None:
    """Record that SSH just failed, so the following calls stop trying it.

    Dropping the entry instead would make every call re-probe, and each probe
    waits out the connect timeout; a guest that has only just rebooted stays
    unreachable for longer than one probe anyway. The entry expires by itself.
    """
    _HEALTH[_health_key(settings, vm_name)] = (time.monotonic(), False)


def chosen_transport(settings: Any, vm_name: str) -> str:
    """The transport this VM is configured for, in order of who said it.

    A per-VM environment override wins because it is explicit and per-process;
    then the operator's saved choice, which the window's switch writes; then the
    host-wide default.
    """
    key = vm_name.casefold().replace("_", "-")
    override = settings.guest_transport_overrides.get(key)
    if override:
        return str(override)
    path = getattr(settings, "transport_choice_file", None)
    if path:
        saved = TransportChoiceStore(path).get(vm_name)
        if saved:
            return saved
    return settings.guest_transport


def select_transport(settings: Any, backend: Any, vm_name: str) -> GuestTransport:
    """Return the transport for one VM.

    `auto` prefers SSH only when it was provisioned by this tool and answers
    within the health timeout; otherwise it is PowerShell Direct. An explicit
    `ssh` never falls back silently: an unprovisioned VM is an error.
    """
    choice = chosen_transport(settings, vm_name)
    ssh = SshTransport(settings)
    if choice == "ssh":
        ssh.state(vm_name)  # Raises PolicyError when provisioning is missing.
        return ssh
    if choice == "auto" and ssh.is_provisioned(vm_name) and _ssh_healthy(ssh, vm_name):
        return ssh
    return PowerShellDirectTransport(backend, settings)


__all__ = [
    "ExecResult", "FileResult", "GuestTransport", "PowerShellDirectTransport",
    "SshTransport", "TRANSPORTS", "TransportChoiceStore", "chosen_transport",
    "invalidate_health", "note_ssh_down", "select_transport",
]
