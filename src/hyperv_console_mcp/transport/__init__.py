"""Selects the guest transport for a VM from the configured policy."""

from __future__ import annotations

from typing import Any

from ..errors import PolicyError
from .base import ExecResult, FileResult, GuestTransport
from .psdirect import PowerShellDirectTransport

TRANSPORTS = ("auto", "psdirect", "ssh")


def select_transport(settings: Any, backend: Any, vm_name: str) -> GuestTransport:
    """Return the transport for one VM.

    `auto` prefers SSH only when it was provisioned by this tool and answers
    quickly; until SSH provisioning exists, `auto` is PowerShell Direct. An
    explicit `ssh` never falls back silently.
    """
    choice = settings.guest_transport_for(vm_name)
    if choice == "ssh":
        raise PolicyError(
            "SSH transport is configured but not available in this build; "
            "set HYPERV_MCP_GUEST_TRANSPORT=psdirect or auto."
        )
    return PowerShellDirectTransport(backend, settings)


__all__ = [
    "ExecResult", "FileResult", "GuestTransport", "PowerShellDirectTransport",
    "TRANSPORTS", "select_transport",
]
