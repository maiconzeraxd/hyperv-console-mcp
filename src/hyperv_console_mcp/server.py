"""MCP stdio server. No protocol data or diagnostics are written to stdout."""

from __future__ import annotations

import json
import logging
import sys
from functools import partial
from typing import Annotated, Any, Callable, Literal, TypeVar, Union

import anyio
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__
from .config import TOOLSETS, Settings
from .errors import HyperVError
from .hostwmi import create_backend
from .service import HyperVService

T = TypeVar("T")
LOGGER = logging.getLogger("hyperv_console_mcp")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
MUTATION = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)

# Which family a tool belongs to, decided by its name. The order matters: the
# specific prefixes must come before the general ones they extend.
TOOLSET_PREFIXES: tuple[tuple[str, str], ...] = (
    ("vm_inject_nmi", "kd"),
    ("guest_ssh_", "ssh"),
    ("guest_credential", "credentials"),
    ("guest_autologon_configure", "config"),
    ("guest_config_", "config"),
    ("guest_", "guest"),
    ("console_", "console"),
    ("media_", "media"),
    ("serial_", "serial"),
    ("debug_", "debug"),
    ("kd_", "kd"),
    ("lab_", "lab"),
)


def toolset_for(name: str) -> str:
    """The toolset that owns ``name``; every tool belongs to exactly one."""
    for prefix, toolset in TOOLSET_PREFIXES:
        if name.startswith(prefix):
            return toolset
    return "core"


class TextAction(BaseModel):
    """Type printable ASCII, tab, CR or LF through the synthetic keyboard."""

    type: Literal["text"]
    text: str


class KeysAction(BaseModel):
    """Press and release a chord, for example ["ctrl", "s"] or ["enter"]."""

    type: Literal["keys"]
    keys: list[str]


class KeyStateAction(BaseModel):
    """Hold or release one key, for drags and modifier keys."""

    type: Literal["key_state"]
    key: str
    down: bool


class MouseButtonsAction(BaseModel):
    """Set the synthetic mouse button states explicitly."""

    type: Literal["mouse_buttons"]
    left: bool = False
    right: bool = False
    middle: bool = False


class MoveAction(BaseModel):
    """Move to absolute guest-display pixels, not host-window coordinates."""

    type: Literal["move"]
    x: int
    y: int


class ClickAction(BaseModel):
    """Click a button, optionally moving to guest-display x/y first."""

    type: Literal["click"]
    button: Literal["left", "right", "middle"] = "left"
    x: int | None = None
    y: int | None = None


class ScrollAction(BaseModel):
    """Apply a relative wheel delta; positive normally scrolls up."""

    type: Literal["scroll"]
    delta: int


class WaitAction(BaseModel):
    """Pause inside the batch; the cumulative wait budget is 30 s."""

    type: Literal["wait"]
    milliseconds: int


class CtrlAltDeleteAction(BaseModel):
    """Send the Hyper-V secure-attention sequence."""

    type: Literal["ctrl_alt_delete"]


ConsoleAction = Annotated[
    Union[
        TextAction, KeysAction, KeyStateAction, MouseButtonsAction,
        MoveAction, ClickAction, ScrollAction, WaitAction, CtrlAltDeleteAction,
    ],
    Field(discriminator="type"),
]



def _failure(exc: HyperVError) -> str:
    """``code: message``, and the fix as one JSON line when the error carries one.

    The fix is what turns a refusal into a next step: either a tool call the
    caller can make now, or a note that only the operator can close the gap.
    It is rendered as JSON so a client can parse it instead of reading prose.
    """
    if not exc.fix:
        return f"{exc.code}: {exc}"
    return f"{exc.code}: {exc}\nfix: {json.dumps(exc.fix, ensure_ascii=False, sort_keys=True)}"


async def _invoke(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return await anyio.to_thread.run_sync(partial(function, *args, **kwargs))
    except HyperVError as exc:
        raise ToolError(_failure(exc)) from None
    except Exception as exc:
        LOGGER.error("Unexpected tool failure (%s).", type(exc).__name__)
        raise ToolError("internal_error: unexpected server failure") from None


def create_server(
    service: HyperVService | None = None,
) -> MCPServer:
    service = service or HyperVService(create_backend(), Settings.from_env())
    mcp = MCPServer(
        name="hyperv-console-mcp",
        version=__version__,
        instructions=(
            "Manage only explicitly allowed Hyper-V VMs. Read state before acting. "
            "Forced power, checkpoint restore/deletion, and file overwrite need exact "
            "confirmation values. Console input can change the guest immediately. "
            "Do not infer screenshot pixels as guest mouse coordinates when VMConnect scales."
        ),
    )

    enabled = service.settings.toolsets

    def tool(name: str, *, annotations: ToolAnnotations, description: str) -> Callable:
        # A tool outside the enabled toolsets is never registered, so its schema
        # never reaches the conversation. The decorator still returns the
        # function, which keeps the definition readable.
        if toolset_for(name) not in enabled:
            return lambda function: function
        return mcp.tool(
            name=name,
            description=description,
            annotations=annotations,
            structured_output=True,
        )

    def image_tool(name: str, *, annotations: ToolAnnotations, description: str) -> Callable:
        if toolset_for(name) not in enabled:
            return lambda function: function
        return mcp.tool(
            name=name,
            description=description,
            annotations=annotations,
            structured_output=False,
        )

    @tool("hyperv_host_status", annotations=READ_ONLY, description="Read Hyper-V host and service health, plus which tool families this server exposes.")
    async def host_status() -> dict[str, Any]:
        result = await _invoke(service.host_status)
        # A capability that is not registered cannot be called, so the host
        # report names the families that are off instead of leaving the model
        # to guess why a tool it expects is missing.
        result["toolsets"] = {
            "enabled": sorted(enabled),
            "disabled": sorted(TOOLSETS - enabled),
        }
        return result

    @tool("vm_list", annotations=READ_ONLY, description="List VMs visible through the optional allowlist.")
    async def vm_list() -> dict[str, Any]:
        return await _invoke(service.list_vms)

    @tool(
        "vm_get", annotations=READ_ONLY,
        description="Get VM state, devices, memory, network, and checkpoint count. A running VM "
        "also carries guest_facts, the computer name and OS the guest publishes through the "
        "key-value pair exchange, which needs no guest credential. With wait_for set to a list of "
        "states, poll until the VM reaches one of them instead and report matched=false on "
        "timeout; waiting never changes power state.",
    )
    async def vm_get(
        vm_name: str,
        wait_for: list[str] | None = None,
        timeout_seconds: int = 60,
        poll_interval_ms: int = 500,
    ) -> dict[str, Any]:
        if wait_for:
            return await _invoke(
                service.vm_wait, vm_name, wait_for, timeout_seconds, poll_interval_ms
            )
        return await _invoke(service.get_vm, vm_name)

    @tool(
        "vm_power", annotations=MUTATION,
        description="Start, gracefully shut down, save, pause, resume, forcibly turn off, or reset a VM. "
        "turn_off/reset require confirm_vm equal to vm_name. Requires ALLOW_POWER and an allowlist.",
    )
    async def vm_power(
        vm_name: str,
        action: Literal["start", "shutdown", "save", "pause", "resume", "turn_off", "reset"],
        confirm_vm: str | None = None,
    ) -> dict[str, Any]:
        return await _invoke(service.power, vm_name, action, confirm_vm)

    @tool(
        "operation_status", annotations=READ_ONLY,
        description="Track a background power operation (shutdown or save) by the operation_id returned by vm_power.",
    )
    async def operation_status(operation_id: str) -> dict[str, Any]:
        return await _invoke(service.operation_status, operation_id)

    @tool("checkpoint_list", annotations=READ_ONLY, description="List checkpoint IDs, names, parents, and creation times.")
    async def checkpoint_list(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.list_checkpoints, vm_name)

    @tool(
        "checkpoint", annotations=MUTATION,
        description="Create, restore or delete a checkpoint. action='create' takes name and requires "
        "ALLOW_CHECKPOINTS; 'restore' discards later guest state; 'delete' merges the checkpoint's "
        "disks and does not include its children. restore and delete need confirm_vm and "
        "confirm_snapshot_id equal to the VM name and the checkpoint ID.",
    )
    async def checkpoint(
        vm_name: str,
        action: Literal["create", "restore", "delete"],
        name: str | None = None,
        snapshot_id: str | None = None,
        confirm_vm: str | None = None,
        confirm_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        if action == "create":
            if not name:
                raise ToolError("validation_error: action='create' needs name.")
            return await _invoke(service.create_checkpoint, vm_name, name)
        if not snapshot_id:
            raise ToolError(f"validation_error: action='{action}' needs snapshot_id.")
        if action == "restore":
            return await _invoke(
                service.restore_checkpoint, vm_name, snapshot_id, confirm_vm, confirm_snapshot_id
            )
        return await _invoke(
            service.delete_checkpoint, vm_name, snapshot_id, confirm_vm, confirm_snapshot_id
        )

    @tool(
        "guest_report", annotations=READ_ONLY,
        description="Read guest state through PowerShell Direct. what='status' reports OS, boot time, "
        "memory, system-drive free space and kernel debug settings; 'processes' lists up to 200 "
        "processes; 'services' lists up to 200 services, stopped ones only with include_stopped=true; "
        "'events' reads recent System, Application, Security or Setup records. Requires ALLOW_GUEST "
        "and guest credentials.",
    )
    async def guest_report(
        vm_name: str,
        what: Literal["status", "processes", "services", "events"] = "status",
        name_contains: str = "",
        include_stopped: bool = False,
        log_name: Literal["System", "Application", "Security", "Setup"] = "System",
        max_events: int = 30,
        event_id: int | None = None,
        level: int | None = None,
        hours: int = 72,
    ) -> dict[str, Any]:
        if what == "processes":
            return await _invoke(service.guest_processes, vm_name, name_contains)
        if what == "services":
            return await _invoke(service.guest_services, vm_name, name_contains, include_stopped)
        if what == "events":
            return await _invoke(
                service.guest_events, vm_name, log_name, max_events, event_id, level, hours
            )
        return await _invoke(service.guest_status, vm_name)

    @tool(
        "guest_exec", annotations=MUTATION,
        description="Run PowerShell in the guest through PowerShell Direct. Credentials come only from the server environment. "
        "Returns combined output, exit code, and truncation status. A host-side timeout may leave the guest command running.",
    )
    async def guest_exec(vm_name: str, command: str) -> dict[str, Any]:
        return await _invoke(service.guest_exec, vm_name, command)

    @tool(
        "guest_read_file", annotations=READ_ONLY,
        description="Read an absolute guest file path as base64, with SHA-256. Default limit 64 KiB; maximum 8 MiB. For large files, request bounded chunks through the lab APIs rather than inflating model context.",
    )
    async def guest_read_file(
        vm_name: str, guest_path: str, max_bytes: int = 65_536
    ) -> dict[str, Any]:
        return await _invoke(service.guest_read_file, vm_name, guest_path, max_bytes)

    @tool(
        "guest_write_file", annotations=MUTATION,
        description="Write base64 content to an absolute guest path. Existing files require overwrite=true and confirm_path equal to guest_path.",
    )
    async def guest_write_file(
        vm_name: str,
        guest_path: str,
        content_base64: str,
        overwrite: bool = False,
        confirm_path: str | None = None,
        create_parent: bool = False,
    ) -> dict[str, Any]:
        return await _invoke(
            service.guest_write_file, vm_name, guest_path, content_base64,
            overwrite=overwrite, confirm_path=confirm_path, create_parent=create_parent,
        )

    @tool(
        "guest_put_file", annotations=MUTATION,
        description="Copy a host file under HYPERV_MCP_TRANSFER_ROOT into the guest and verify its SHA-256. Use this instead of base64 for anything larger than a few KiB.",
    )
    async def guest_put_file(
        vm_name: str,
        local_path: str,
        guest_path: str,
        overwrite: bool = False,
        confirm_path: str | None = None,
    ) -> dict[str, Any]:
        return await _invoke(
            service.guest_put_file, vm_name, local_path, guest_path,
            overwrite=overwrite, confirm_path=confirm_path,
        )

    @tool(
        "guest_get_file", annotations=MUTATION,
        description="Copy a guest file to a host path under HYPERV_MCP_TRANSFER_ROOT and verify its SHA-256.",
    )
    async def guest_get_file(
        vm_name: str,
        guest_path: str,
        local_path: str,
        overwrite: bool = False,
        confirm_path: str | None = None,
    ) -> dict[str, Any]:
        return await _invoke(
            service.guest_get_file, vm_name, guest_path, local_path,
            overwrite=overwrite, confirm_path=confirm_path,
        )

    @tool(
        "guest_ssh_provision", annotations=MUTATION,
        description="Provision SSH in the guest over PowerShell Direct: a dedicated internal switch, an ed25519 key, key-only sshd, and a TCP 22 rule scoped to the host's lab address. Never uses an external switch. Requires confirm_vm.",
    )
    async def guest_ssh_provision(
        vm_name: str, confirm_vm: str, use_existing_adapter: bool = False
    ) -> dict[str, Any]:
        return await _invoke(
            service.guest_ssh_provision, vm_name, confirm_vm,
            use_existing_adapter=use_existing_adapter,
        )

    @tool(
        "guest_ssh_status", annotations=READ_ONLY,
        description="Report SSH provisioning: pinned host key, sshd service and config, authorized key, firewall rule, guest address, and whether the connection answers.",
    )
    async def guest_ssh_status(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.guest_ssh_status, vm_name)

    @tool(
        "guest_ssh_deprovision", annotations=MUTATION,
        description="Remove the SSH firewall rule, the tool's authorized key, the pinned host key and the lab adapter if provisioning created it. Requires confirm_vm.",
    )
    async def guest_ssh_deprovision(vm_name: str, confirm_vm: str) -> dict[str, Any]:
        return await _invoke(service.guest_ssh_deprovision, vm_name, confirm_vm)

    @tool(
        "guest_credential_status", annotations=READ_ONLY,
        description="Report where the guest credential comes from (Credential Manager or environment), the account name, and whether PowerShell Direct and SSH accept it. Never returns the password.",
    )
    async def guest_credential_status(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.guest_credential_status, vm_name)

    @tool(
        "guest_credential", annotations=MUTATION,
        description="Maintain the stored guest credential. action='rotate' generates a new random "
        "password, sets it over PowerShell Direct, proves a fresh session accepts it and only then "
        "stores it in the Windows Credential Manager, restoring the previous password if the new one "
        "fails; action='forget' deletes the stored credential without changing the guest password. "
        "Both need confirm_vm.",
    )
    async def guest_credential(
        vm_name: str, action: Literal["rotate", "forget"], confirm_vm: str
    ) -> dict[str, Any]:
        if action == "rotate":
            return await _invoke(service.guest_password_rotate, vm_name, confirm_vm)
        return await _invoke(service.guest_credential_forget, vm_name, confirm_vm)

    @tool(
        "guest_autologon_configure", annotations=MUTATION,
        description="Boot the guest straight to the desktop: autologon with the password kept in LSA private data, plus independent toggles for no lock screen, no Ctrl+Alt+Del, no standby/screen blanking/console lock, no UAC consent prompt for administrators, and no automatic update reboot. Every change is recorded with the value it replaced. Restarts only when restart=true. Requires confirm_vm.",
    )
    async def guest_autologon_configure(
        vm_name: str,
        enabled: bool,
        confirm_vm: str,
        restart: bool = False,
        lock_screen: bool | None = None,
        disable_cad: bool | None = None,
        power: bool | None = None,
        uac: bool | None = None,
        windows_update: bool | None = None,
    ) -> dict[str, Any]:
        return await _invoke(
            service.guest_autologon_configure, vm_name, enabled, confirm_vm,
            restart=restart, lock_screen=lock_screen, disable_cad=disable_cad,
            power=power, uac=uac, windows_update=windows_update,
        )

    @tool(
        "guest_config_status", annotations=READ_ONLY,
        description="Report the guest's autologon, lock-screen, Ctrl+Alt+Del, power, UAC and update settings, which of them this tool recorded, and any interactive session. Changes no setting; it uploads its helper script if the guest does not have it yet.",
    )
    async def guest_config_status(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.guest_config_status, vm_name)

    @tool(
        "guest_config_revert", annotations=MUTATION,
        description="Restore every setting recorded in the guest's guest-config.json to the value it had before this tool changed it, delete the LSA autologon secret if it was absent, and remove the state file. Restarts only when restart=true. Requires confirm_vm.",
    )
    async def guest_config_revert(
        vm_name: str, confirm_vm: str, restart: bool = False
    ) -> dict[str, Any]:
        return await _invoke(service.guest_config_revert, vm_name, confirm_vm, restart=restart)

    @tool(
        "lab_init", annotations=MUTATION,
        description="Install or verify fixed lab tools in C:\\DriverLab and create package/run/evidence directories. Never executes a payload. Requires confirm_vm.",
    )
    async def lab_init(vm_name: str, confirm_vm: str) -> dict[str, Any]:
        return await _invoke(service.lab_init, vm_name, confirm_vm)

    @tool("lab_diagnostics", annotations=READ_ONLY, description="Read guest boot, Secure Boot, VBS/HVCI, test-signing, debugger, disk, crash-dump and Driver Verifier configuration. The verifier numbers come from the registry, so the answer does not depend on the guest's language.")
    async def lab_diagnostics(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.lab_diagnostics, vm_name)

    @tool(
        "lab_stage", annotations=MUTATION,
        description="Stage a hash-verified package from a subdirectory of HYPERV_MCP_LAB_ROOT. Supports chunked large files and never executes staged content.",
    )
    async def lab_stage(vm_name: str, source_directory: str, package_id: str | None = None) -> dict[str, Any]:
        return await _invoke(service.lab_stage, vm_name, source_directory, package_id)

    @tool("lab_verify", annotations=READ_ONLY, description="Re-verify all package files and the exact manifest SHA-256 in the guest without executing the package.")
    async def lab_verify(vm_name: str, package_id: str, expected_manifest_sha256: str) -> dict[str, Any]:
        return await _invoke(service.lab_verify, vm_name, package_id, expected_manifest_sha256)

    @tool(
        "lab_collect", annotations=MUTATION,
        description="Collect a new run's logs, events, diagnostics and dumps into HYPERV_MCP_EVIDENCE_ROOT. A kernel dump is opt-in; no new test is started.",
    )
    async def lab_collect(
        vm_name: str, package_id: str, run_id: str, include_kernel_dump: bool = False
    ) -> dict[str, Any]:
        return await _invoke(service.lab_collect, vm_name, package_id, run_id, include_kernel_dump)

    @tool(
        "lab_run", annotations=MUTATION,
        description="Lab profile only. Create checkpoint pre-<run_id>, re-verify the package, run its Run-Lab.ps1 in the guest, and classify the outcome as ok, failed, not_verified, bugcheck, hang, or timeout. The approval phrase is generated here; the VM must already be Running.",
    )
    async def lab_run(
        vm_name: str,
        package_id: str,
        run_id: str,
        expected_manifest_sha256: str,
        timeout_seconds: int = 600,
        checkpoint_before: bool = True,
    ) -> dict[str, Any]:
        return await _invoke(
            service.lab_run, vm_name, package_id, run_id, expected_manifest_sha256,
            timeout_seconds, checkpoint_before,
        )

    @tool(
        "lab_cycle", annotations=MUTATION,
        description="Lab profile only. Optionally attach kd (debugger='serial' or 'kdnet'), run the package, "
        "collect evidence into HYPERV_MCP_EVIDENCE_ROOT, then optionally restore checkpoint pre-<run_id>. "
        "With a debugger, a bugcheck is reported as a bugcheck with the parsed !analyze -v instead of a hang; "
        "a crash is triaged from the collected dump either way, and the cycle writes lab-cycle.json beside "
        "the evidence so runs of the same package can be compared.",
    )
    async def lab_cycle(
        vm_name: str,
        package_id: str,
        run_id: str,
        expected_manifest_sha256: str,
        timeout_seconds: int = 600,
        checkpoint_before: bool = True,
        revert_after: bool = False,
        include_kernel_dump: bool = True,
        debugger: Literal["none", "serial", "kdnet"] = "none",
    ) -> dict[str, Any]:
        return await _invoke(
            service.lab_cycle, vm_name, package_id, run_id, expected_manifest_sha256,
            timeout_seconds, checkpoint_before, revert_after, include_kernel_dump,
            debugger,
        )

    @tool(
        "lab_verifier", annotations=MUTATION,
        description="Lab profile only. Turn Driver Verifier on for named test drivers (or for every "
        "driver), turn it off, or restore the setting this tool replaced. flags is 'standard', 'all', "
        "or a numeric mask; drivers are module names such as mydriver.sys, never paths. Every change "
        "is recorded in the guest's verifier.json and revertible, takes effect on the next restart, "
        "and this tool never restarts the VM. Requires confirm_vm.",
    )
    async def lab_verifier(
        vm_name: str,
        action: Literal["enable", "disable", "revert"],
        confirm_vm: str,
        drivers: list[str] | None = None,
        flags: str = "standard",
    ) -> dict[str, Any]:
        return await _invoke(
            service.lab_verifier, vm_name, action, confirm_vm, drivers, flags
        )

    @tool(
        "lab_driver_service", annotations=MUTATION,
        description="Install, start, stop or remove a test kernel-driver service in the guest, or read "
        "the services this tool installed. image_path must be a .sys file already staged under "
        r"C:\DriverLab" "; start_type is demand, system, boot or auto. Signature enforcement is never "
        "bypassed: a driver the guest refuses is reported with its Win32 error and NTSTATUS. Reading "
        "the state needs no confirmation; the other actions require confirm_vm.",
    )
    async def lab_driver_service(
        vm_name: str,
        action: Literal["install", "start", "stop", "remove", "status"],
        service_name: str = "",
        confirm_vm: str = "",
        image_path: str = "",
        start_type: Literal["demand", "system", "boot", "auto"] = "demand",
    ) -> dict[str, Any]:
        return await _invoke(
            service.lab_driver_service, vm_name, action, service_name, confirm_vm,
            image_path, start_type,
        )

    @tool(
        "lab_guest_configure", annotations=MUTATION,
        description="Prepare the guest for driver work: test signing, kernel debugging, crash dump mode and HVCI/VBS off, each recorded in guest-config.json and revertible with guest_config_revert. Toggles are tri-state. Secure Boot is checked first; secure_boot_off=true only works while the VM is Off. Requires confirm_vm.",
    )
    async def lab_guest_configure(
        vm_name: str,
        confirm_vm: str,
        testsigning: bool | None = None,
        kernel_debug: bool | None = None,
        crash_dump: str | None = None,
        disable_hvci: bool | None = None,
        secure_boot_off: bool = False,
        restart: bool = False,
    ) -> dict[str, Any]:
        return await _invoke(
            service.lab_guest_configure, vm_name, confirm_vm,
            testsigning=testsigning, kernel_debug=kernel_debug, crash_dump=crash_dump,
            disable_hvci=disable_hvci, secure_boot_off=secure_boot_off, restart=restart,
        )

    @tool(
        "vm_inject_nmi", annotations=MUTATION,
        description="Inject a non-maskable interrupt into a Running VM so a hung guest bugchecks into the debugger. Requires confirm_vm and HYPERV_MCP_ALLOW_DEBUGGER.",
    )
    async def vm_inject_nmi(vm_name: str, confirm_vm: str) -> dict[str, Any]:
        return await _invoke(service.vm_inject_nmi, vm_name, confirm_vm)

    @tool("events_snapshot", annotations=READ_ONLY, description="Read recent operation lifecycle events from the shared service, without commands, passwords, typed text, or file contents.")
    async def events_snapshot(after_sequence: int = 0) -> dict[str, Any]:
        if not isinstance(after_sequence, int) or isinstance(after_sequence, bool) or after_sequence < 0:
            from .errors import ValidationError
            raise ValidationError("after_sequence must be a non-negative integer.")
        return await _invoke(service.event_snapshot, after_sequence)

    @tool(
        "console_info", annotations=READ_ONLY,
        description="Read guest video-head resolutions for mouse coordinate planning.",
    )
    async def console_info(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.console_info, vm_name)

    @tool(
        "console_input", annotations=MUTATION,
        description="Send ordered input to the guest console in one bridge request: text, key "
        "chords, explicit key or mouse-button states, mouse move/click/scroll, waits, and "
        "Ctrl+Alt+Delete. A single action is a list of one. Text is limited to printable ASCII, "
        "tab, CR and LF; use guest_exec for Unicode. The batch stops at the first failure. No "
        "VMConnect focus is required. Requires ALLOW_CONSOLE.",
    )
    async def console_input(vm_name: str, actions: list[ConsoleAction]) -> dict[str, Any]:
        prepared = [action.model_dump(exclude_none=True) for action in actions]
        return await _invoke(service.console_batch, vm_name, prepared)

    @tool(
        "console_wait_for_change", annotations=READ_ONLY,
        description="Wait until the guest display changes, polling a downscaled console thumbnail "
        "instead of screenshotting in a loop. region is [x, y, width, height] in guest pixels. "
        "Requires ALLOW_SCREENSHOTS; returns no image.",
    )
    async def console_wait_for_change(
        vm_name: str,
        timeout_ms: int = 30_000,
        region: list[int] | None = None,
        min_change: float = 0.01,
    ) -> dict[str, Any]:
        return await _invoke(
            service.console_wait_for_change, vm_name, timeout_ms, region, min_change
        )

    @tool(
        "media_list", annotations=READ_ONLY,
        description="List VM DVD drives, mounted ISO paths, controller locations, generation, and power state.",
    )
    async def media_list(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.media_list, vm_name)

    @tool(
        "media_mount", annotations=MUTATION,
        description="Mount an existing host ISO in an existing VM DVD drive while the VM is Off, optionally making it first boot device. "
        "Never stops the VM automatically. Requires ALLOW_MEDIA and confirm_vm equal to vm_name.",
    )
    async def media_mount(
        vm_name: str,
        iso_path: str,
        controller_location: int,
        confirm_vm: str,
        boot_first: bool = False,
        controller_number: int = 0,
    ) -> dict[str, Any]:
        return await _invoke(
            service.media_mount, vm_name, iso_path, controller_location,
            confirm_vm, boot_first, controller_number,
        )

    @tool(
        "media_eject", annotations=MUTATION,
        description="Eject an existing VM DVD drive while the VM is Off. Requires ALLOW_MEDIA and confirm_vm equal to vm_name.",
    )
    async def media_eject(
        vm_name: str, controller_location: int, confirm_vm: str, controller_number: int = 0
    ) -> dict[str, Any]:
        return await _invoke(
            service.media_eject, vm_name, controller_location, confirm_vm, controller_number
        )

    @tool(
        "serial_status", annotations=READ_ONLY,
        description="Read COM1 configuration and whether this server has an attached serial-console reader.",
    )
    async def serial_status(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.serial_status, vm_name)

    @tool(
        "serial_connect", annotations=MUTATION,
        description="Attach a dedicated reader to the existing COM1 named pipe of a Running VM and append output to a local .log file. "
        "Requires ALLOW_DEBUGGER and confirm_vm; does not configure the port or guest.",
    )
    async def serial_connect(
        vm_name: str, pipe_name: str, log_path: str, confirm_vm: str
    ) -> dict[str, Any]:
        return await _invoke(service.serial_connect, vm_name, pipe_name, log_path, confirm_vm)

    @tool(
        "serial_read", annotations=READ_ONLY,
        description="Read the retained tail of an attached serial console. Requires ALLOW_DEBUGGER.",
    )
    async def serial_read(vm_name: str, tail_chars: int = 32_768) -> dict[str, Any]:
        return await _invoke(service.serial_read, vm_name, tail_chars)

    @tool(
        "serial_send", annotations=MUTATION,
        description="Write UTF-8 text to an attached COM1 serial console, with CRLF appended by default.",
    )
    async def serial_send(
        vm_name: str, text: str, append_newline: bool = True
    ) -> dict[str, Any]:
        return await _invoke(service.serial_send, vm_name, text, append_newline)

    @tool(
        "serial_disconnect", annotations=MUTATION,
        description="Detach this server's serial reader and close its pipe handle without changing the VM COM port.",
    )
    async def serial_disconnect(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.serial_disconnect, vm_name)

    @tool(
        "debug_status", annotations=READ_ONLY,
        description="Read kernel-debug setup for a VM. transport='serial' reports the Hyper-V COM port "
        "debug configuration and local WinDbg availability; transport='kdnet' reports the prepared "
        "port, host address, switch, firewall rule and whether the key is stored. Never returns the key.",
    )
    async def debug_status(
        vm_name: str, transport: Literal["serial", "kdnet"] = "serial"
    ) -> dict[str, Any]:
        if transport == "kdnet":
            return await _invoke(service.debug_kdnet_status, vm_name)
        return await _invoke(service.debug_serial_status, vm_name)

    @tool(
        "debug_setup", annotations=MUTATION,
        description="Prepare or remove kernel debugging. transport='serial' with action='prepare' "
        "points COM1 at a local named pipe while the VM is Off, and with action='guest' sets fixed "
        "COM1/115200 BCD debugging over PowerShell Direct while it is Running. transport='kdnet' with "
        "action='prepare' attaches the internal lab switch, allocates a UDP port in 50000-50039, sets "
        "guest BCD, adds a host firewall rule for kd.exe on that port and stores the generated key; "
        "with action='deprovision' it removes the rule, the key and the local state. A deliberate "
        "later reboot is required. Requires ALLOW_DEBUGGER and confirm_vm.",
    )
    async def debug_setup(
        vm_name: str,
        transport: Literal["serial", "kdnet"],
        confirm_vm: str,
        action: Literal["prepare", "guest", "deprovision"] = "prepare",
        pipe_name: str | None = None,
        port: int | None = None,
        use_existing_adapter: bool = False,
    ) -> dict[str, Any]:
        if transport == "serial":
            if action == "guest":
                return await _invoke(service.debug_guest_prepare, vm_name, confirm_vm)
            if action == "deprovision":
                raise ToolError(
                    "validation_error: serial debugging has no deprovision step; use kdnet."
                )
            if not pipe_name:
                raise ToolError("validation_error: transport='serial' needs pipe_name.")
            return await _invoke(service.debug_serial_prepare, vm_name, pipe_name, confirm_vm)
        if action == "guest":
            raise ToolError(
                "validation_error: KDNET sets its guest BCD as part of action='prepare'."
            )
        if action == "deprovision":
            return await _invoke(service.debug_kdnet_deprovision, vm_name, confirm_vm)
        return await _invoke(
            service.debug_kdnet_prepare, vm_name, confirm_vm, port, use_existing_adapter
        )

    @tool(
        "kd_attach", annotations=MUTATION,
        description="Start kd.exe against a VM over KDNET (default) or the COM1 debugger pipe and keep it "
        "attached. A serial-console reader on the same pipe is detached first. Every session is logged under "
        "the configured kd root. Requires ALLOW_DEBUGGER.",
    )
    async def kd_attach(
        vm_name: str,
        transport: Literal["serial", "kdnet"] = "kdnet",
        break_on_connect: bool = False,
        initial_break: bool = False,
    ) -> dict[str, Any]:
        return await _invoke(
            service.kd_attach, vm_name, transport, break_on_connect, initial_break
        )

    @tool(
        "kd_status", annotations=READ_ONLY,
        description="Read the kd session state machine (disconnected, waiting_for_target, running, broken), "
        "pid, transport, log path and command count for a VM.",
    )
    async def kd_status(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.kd_status, vm_name)

    @tool(
        "kd_detach", annotations=MUTATION,
        description="Detach kd from a VM, resuming the target if it is stopped and closing the log. "
        "Requires ALLOW_DEBUGGER.",
    )
    async def kd_detach(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.kd_detach, vm_name)

    @tool(
        "kd_execution", annotations=MUTATION,
        description="Control target execution: action='break' stops a running target (console break-in "
        "first, then vm_inject_nmi unless nmi=false), action='go' resumes it with g, gh, gn, gc or gu "
        "and waits until it breaks again. Requires ALLOW_DEBUGGER.",
    )
    async def kd_execution(
        vm_name: str,
        action: Literal["break", "go"],
        command: str = "g",
        nmi: bool | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        if action == "break":
            return await _invoke(service.kd_break, vm_name, nmi, timeout_ms)
        return await _invoke(service.kd_go, vm_name, command, timeout_ms)

    @tool(
        "kd_command", annotations=MUTATION,
        description="Run one kd command on an attached session and return its output, delimited by a unique "
        "per-session marker rather than a timeout. Commands that resume the target belong in "
        "kd_execution. Requires ALLOW_DEBUGGER.",
    )
    async def kd_command(
        vm_name: str, command: str, timeout_ms: int | None = None
    ) -> dict[str, Any]:
        return await _invoke(service.kd_command, vm_name, command, timeout_ms)

    @tool(
        "kd_wait_event", annotations=READ_ONLY,
        description="Wait for the next kernel event: break, bugcheck, module load, target exit, or timeout. "
        "Read-only: it observes the attached session and never sends a command.",
    )
    async def kd_wait_event(vm_name: str, timeout_ms: int | None = None) -> dict[str, Any]:
        return await _invoke(service.kd_wait_event, vm_name, timeout_ms)

    @tool(
        "kd_breakpoint", annotations=MUTATION,
        description="Manage breakpoints in the attached session. action='set' sets a software breakpoint "
        "(bp) or, with deferred=true, a deferred one (bu) with an optional command string; action='clear' "
        "removes every breakpoint (bc *); action='on_load' arms sxe ld:<module> or, with enabled=false, "
        "sxd ld:<module>, so a driver load is caught before its entry point runs. Requires ALLOW_DEBUGGER.",
    )
    async def kd_breakpoint(
        vm_name: str,
        action: Literal["set", "clear", "on_load"],
        target: str | None = None,
        module: str | None = None,
        deferred: bool = False,
        command: str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        if action == "set":
            if not target:
                raise ToolError("validation_error: action='set' needs target.")
            return await _invoke(
                service.kd_set_breakpoint, vm_name, target, deferred, command
            )
        if action == "on_load":
            if not module:
                raise ToolError("validation_error: action='on_load' needs module.")
            return await _invoke(service.kd_break_on_load, vm_name, module, enabled)
        return await _invoke(service.kd_clear_breakpoints, vm_name)

    @tool(
        "kd_inspect", annotations=READ_ONLY,
        description="Read a debug target without changing it. what='stack' returns the call stack (k) "
        "of the broken target, depth frames deep; 'modules' lists loaded modules with base addresses "
        "and symbols (lm); 'analyze' runs !analyze -v on the attached session and returns the parsed "
        "bugcheck code, parameters, blamed driver, bucket id and top frames; 'dump' analyzes a host "
        ".dmp under the evidence root with kd -z instead of the live session.",
    )
    async def kd_inspect(
        vm_name: str,
        what: Literal["stack", "modules", "analyze", "dump"] = "analyze",
        depth: int = 20,
        dump_path: str | None = None,
        commands: list[str] | None = None,
    ) -> dict[str, Any]:
        if what == "stack":
            return await _invoke(service.kd_stack, vm_name, depth)
        if what == "modules":
            return await _invoke(service.kd_modules, vm_name)
        if what == "dump":
            if not dump_path:
                raise ToolError("validation_error: what='dump' needs dump_path.")
            return await _invoke(service.kd_dump_open, vm_name, dump_path, commands)
        return await _invoke(service.kd_analyze, vm_name)

    @image_tool(
        name="console_screenshot",
        description="PNG screenshot of the guest display from the Hyper-V console thumbnail: no "
        "VMConnect window or interactive session needed, and at scale 1 its pixels are the same "
        "coordinates console_input's move action uses. source='vmconnect' captures an existing "
        "visible VMConnect window instead. Requires ALLOW_SCREENSHOTS and an allowlist.",
        annotations=READ_ONLY,
    )
    async def console_screenshot(
        vm_name: str,
        source: Literal["thumbnail", "vmconnect"] = "thumbnail",
        region: Literal["window", "client"] = "window",
        max_width: int = 1600,
    ) -> CallToolResult:
        def perform() -> Any:
            return service.capture_screenshot(vm_name, source, region, max_width)

        screenshot = await _invoke(perform)
        metadata_text = json.dumps(screenshot.metadata, ensure_ascii=False)
        return CallToolResult(
            content=[
                TextContent(type="text", text=metadata_text),
                Image(data=screenshot.png, format="png").to_image_content(),
            ],
            structuredContent=screenshot.metadata,
        )

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    settings = Settings.from_env()
    if not settings.shared_service:
        from .events import EVENTS, EventSink
        service = HyperVService(EventSink(create_backend(), EVENTS), settings)
    else:
        from .service.host import ensure_running
        from .service.remote import ServiceProxy
        # A thin client of the one host that owns the workers and the sessions,
        # so a KD or serial session this server opens is visible to the window.
        service = ServiceProxy(ensure_running(), settings)
    create_server(service).run(transport="stdio")


if __name__ == "__main__":
    main()
