"""The worker's actions as service calls: what replaced the operator's scripts.

``Start-NorthLabWorker.ps1`` and ``Invoke-NorthLab.ps1`` were a personal
convenience around this service: bring a guest up, stage a package, sync
symbols, take a checkpoint, reboot, run the live test. Every one of those is a
method the service already had — with no prompt in it, because the credential
comes from the vault as a ``PSCredential`` built in the bridge, the destructive
cmdlets carry ``-Confirm:$false``, and every host runs ``-NonInteractive``.

So the scripts are not needed, and nothing here calls one. This table is the
audit: one row per action the scripts performed, and the service methods that
perform it now. ``tests/test_lab_worker.py`` fails if a row names a method the
façade does not have, and if the table and ``docs/PROJECT.md`` disagree — a
mapping that only lives in prose rots.

The MCP tool names differ from the façade for a few of these (``power`` is
``vm_power``, ``create_checkpoint`` is ``checkpoint``); the table names the
service method, because that is what the scripts were doing and what the
window calls.
"""

from __future__ import annotations

WORKER_ACTIONS: dict[str, tuple[str, ...]] = {
    # Status: is the host there, is the VM up, is the guest reachable.
    "Status": (
        "host_status",
        "get_vm",
        "guest_status",
        "guest_transport_status",
        "lab_diagnostics",
    ),
    # StagePackage: hash-verified copy from the lab root into the guest.
    "StagePackage": ("lab_stage", "guest_publish_package"),
    # SyncSymbols: what the debugger resolved, and from which PDB.
    "SyncSymbols": ("kd_symbols",),
    # Checkpoint: take one, and see the tree afterwards.
    "Checkpoint": ("create_checkpoint", "list_checkpoints"),
    # Reboot: reset is a restart; shutdown is the gentler one.
    "Reboot": ("power",),
    # LiveTest: run the staged package, and the whole cycle around it.
    "LiveTest": ("lab_run", "lab_cycle"),
}

__all__ = ["WORKER_ACTIONS"]
