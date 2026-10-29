"""MCP stdio server. No protocol data or diagnostics are written to stdout."""

from __future__ import annotations

import json
import logging
import sys
from functools import partial
from typing import Any, Callable, Literal, TypeVar

import anyio
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from . import __version__
from .backend import GuestSplitBackend
from .capture import VMConnectCapture
from .config import Settings
from .errors import HyperVError
from .service import HyperVService

T = TypeVar("T")
LOGGER = logging.getLogger("hyperv_console_mcp")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
MUTATION = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)


async def _invoke(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return await anyio.to_thread.run_sync(partial(function, *args, **kwargs))
    except HyperVError as exc:
        raise ToolError(f"{exc.code}: {exc}") from None
    except Exception as exc:
        LOGGER.error("Unexpected tool failure (%s).", type(exc).__name__)
        raise ToolError("internal_error: unexpected server failure") from None


def create_server(
    service: HyperVService | None = None,
    capture: VMConnectCapture | None = None,
) -> MCPServer:
    service = service or HyperVService(GuestSplitBackend(), Settings.from_env())
    capture = capture or VMConnectCapture()
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

    def tool(name: str, *, annotations: ToolAnnotations, description: str) -> Callable:
        return mcp.tool(
            name=name,
            description=description,
            annotations=annotations,
            structured_output=True,
        )

    @tool("hyperv_host_status", annotations=READ_ONLY, description="Read Hyper-V host and service health.")
    async def host_status() -> dict[str, Any]:
        return await _invoke(service.host_status)

    @tool("vm_list", annotations=READ_ONLY, description="List VMs visible through the optional allowlist.")
    async def vm_list() -> dict[str, Any]:
        return await _invoke(service.list_vms)

    @tool("vm_get", annotations=READ_ONLY, description="Get VM state, devices, memory, network, and checkpoint count.")
    async def vm_get(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.get_vm, vm_name)

    @tool(
        "vm_wait", annotations=READ_ONLY,
        description="Poll until a VM reaches one of the requested states. Returns matched=false on timeout and never changes power state.",
    )
    async def vm_wait(
        vm_name: str, states: list[str], timeout_seconds: int = 60, poll_interval_ms: int = 500
    ) -> dict[str, Any]:
        return await _invoke(service.vm_wait, vm_name, states, timeout_seconds, poll_interval_ms)

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
        "checkpoint_create", annotations=MUTATION,
        description="Create a manual checkpoint. Requires ALLOW_CHECKPOINTS and an allowlist.",
    )
    async def checkpoint_create(vm_name: str, name: str) -> dict[str, Any]:
        return await _invoke(service.create_checkpoint, vm_name, name)

    @tool(
        "checkpoint_restore", annotations=MUTATION,
        description="Restore a checkpoint, discarding later guest state. confirm_vm and confirm_snapshot_id must match exactly.",
    )
    async def checkpoint_restore(
        vm_name: str, snapshot_id: str, confirm_vm: str, confirm_snapshot_id: str
    ) -> dict[str, Any]:
        return await _invoke(
            service.restore_checkpoint, vm_name, snapshot_id, confirm_vm, confirm_snapshot_id
        )

    @tool(
        "checkpoint_delete", annotations=MUTATION,
        description="Delete one checkpoint and merge its disks. Child checkpoints are not recursively included. Requires exact confirmations.",
    )
    async def checkpoint_delete(
        vm_name: str, snapshot_id: str, confirm_vm: str, confirm_snapshot_id: str
    ) -> dict[str, Any]:
        return await _invoke(
            service.delete_checkpoint, vm_name, snapshot_id, confirm_vm, confirm_snapshot_id
        )

    @tool(
        "guest_status", annotations=READ_ONLY,
        description="Read guest OS, boot time, memory, system-drive free space, and kernel debug settings through PowerShell Direct.",
    )
    async def guest_status(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.guest_status, vm_name)

    @tool(
        "guest_processes", annotations=READ_ONLY,
        description="List up to 200 guest processes, optionally filtered by process name. Requires ALLOW_GUEST and guest credentials.",
    )
    async def guest_processes(vm_name: str, name_contains: str = "") -> dict[str, Any]:
        return await _invoke(service.guest_processes, vm_name, name_contains)

    @tool(
        "guest_services", annotations=READ_ONLY,
        description="List up to 200 guest services filtered by name; stopped services are excluded by default.",
    )
    async def guest_services(
        vm_name: str, name_contains: str = "", include_stopped: bool = False
    ) -> dict[str, Any]:
        return await _invoke(service.guest_services, vm_name, name_contains, include_stopped)

    @tool(
        "guest_events", annotations=READ_ONLY,
        description="Read recent System, Application, Security, or Setup event records with optional event ID and level filters.",
    )
    async def guest_events(
        vm_name: str,
        log_name: Literal["System", "Application", "Security", "Setup"] = "System",
        max_events: int = 30,
        event_id: int | None = None,
        level: int | None = None,
        hours: int = 72,
    ) -> dict[str, Any]:
        return await _invoke(
            service.guest_events, vm_name, log_name, max_events, event_id, level, hours
        )

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
        "guest_password_rotate", annotations=MUTATION,
        description="Generate a new random guest password, set it over PowerShell Direct, prove a fresh session accepts it, and only then store it in the Windows Credential Manager. Restores the previous password if the new one fails. Requires confirm_vm.",
    )
    async def guest_password_rotate(vm_name: str, confirm_vm: str) -> dict[str, Any]:
        return await _invoke(service.guest_password_rotate, vm_name, confirm_vm)

    @tool(
        "guest_credential_forget", annotations=MUTATION,
        description="Delete the stored guest credential from the Windows Credential Manager. Does not change the guest password. Requires confirm_vm.",
    )
    async def guest_credential_forget(vm_name: str, confirm_vm: str) -> dict[str, Any]:
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

    @tool("lab_diagnostics", annotations=READ_ONLY, description="Read guest boot, Secure Boot, VBS/HVCI, test-signing, debugger, disk, and crash-dump configuration.")
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
        "With a debugger, a bugcheck is reported as a bugcheck with the parsed !analyze -v instead of a hang.",
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

    @tool("events_snapshot", annotations=READ_ONLY, description="Read recent operation lifecycle events from this MCP instance, without commands, passwords, typed text, or file contents.")
    async def events_snapshot(after_sequence: int = 0) -> dict[str, Any]:
        from .events import EVENTS
        if not isinstance(after_sequence, int) or isinstance(after_sequence, bool) or after_sequence < 0:
            from .errors import ValidationError
            raise ValidationError("after_sequence must be a non-negative integer.")
        return EVENTS.snapshot(after_sequence)

    @tool(
        "console_info", annotations=READ_ONLY,
        description="Read guest video-head resolutions for mouse coordinate planning.",
    )
    async def console_info(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.console_info, vm_name)

    @tool(
        "console_type_text", annotations=MUTATION,
        description="Type printable ASCII, tab, CR, or LF through Msvm_Keyboard. Unicode/ABNT2 text is rejected; use guest_exec for Unicode and match the guest keyboard layout. No VMConnect focus is required.",
    )
    async def console_type_text(vm_name: str, text: str) -> dict[str, Any]:
        return await _invoke(service.console_type_text, vm_name, text)

    @tool(
        "console_press_keys", annotations=MUTATION,
        description="Press and release a key chord (for example ['ctrl','s'] or ['enter']) using Hyper-V virtual-key codes.",
    )
    async def console_press_keys(vm_name: str, keys: list[str]) -> dict[str, Any]:
        return await _invoke(service.console_press_keys, vm_name, keys)

    @tool(
        "console_key_state", annotations=MUTATION,
        description="Explicitly press or release one supported key. Release held keys when finished; prefer console_press_keys for ordinary chords.",
    )
    async def console_key_state(vm_name: str, key: str, down: bool) -> dict[str, Any]:
        return await _invoke(service.console_key_state, vm_name, key, down)

    @tool(
        "console_mouse_buttons", annotations=MUTATION,
        description="Set explicit left, right, and middle synthetic-mouse button states for drag operations.",
    )
    async def console_mouse_buttons(
        vm_name: str, left: bool = False, right: bool = False, middle: bool = False
    ) -> dict[str, Any]:
        return await _invoke(service.console_mouse_buttons, vm_name, left, right, middle)

    @tool(
        "console_batch", annotations=MUTATION,
        description="Run up to 32 ordered text, key-chord, click, move, scroll, wait, or Ctrl+Alt+Delete actions in one bridge request. Stops at the first failure.",
    )
    async def console_batch(vm_name: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
        return await _invoke(service.console_batch, vm_name, actions)

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
        "console_ctrl_alt_delete", annotations=MUTATION,
        description="Send the Hyper-V secure-attention sequence to the guest.",
    )
    async def console_ctrl_alt_delete(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.console_ctrl_alt_delete, vm_name)

    @tool(
        "console_mouse_move", annotations=MUTATION,
        description="Move the synthetic mouse to absolute guest-display pixel coordinates, not host-window coordinates.",
    )
    async def console_mouse_move(vm_name: str, x: int, y: int) -> dict[str, Any]:
        return await _invoke(service.console_mouse_move, vm_name, x, y)

    @tool(
        "console_click", annotations=MUTATION,
        description="Click left, right, or middle. Optionally move to guest-display x/y first.",
    )
    async def console_click(
        vm_name: str, button: Literal["left", "right", "middle"] = "left", x: int | None = None, y: int | None = None
    ) -> dict[str, Any]:
        return await _invoke(service.console_click, vm_name, button, x, y)

    @tool(
        "console_scroll", annotations=MUTATION,
        description="Apply a relative synthetic-mouse wheel delta. Positive values normally scroll up.",
    )
    async def console_scroll(vm_name: str, delta: int) -> dict[str, Any]:
        return await _invoke(service.console_scroll, vm_name, delta)

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
        "debug_serial_status", annotations=READ_ONLY,
        description="Read Hyper-V COM port debug configuration and local WinDbg/KDNET availability.",
    )
    async def debug_serial_status(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.debug_serial_status, vm_name)

    @tool(
        "debug_serial_prepare", annotations=MUTATION,
        description="Configure COM1 kernel debugging over a local named pipe while the VM is Off. "
        "Guest BCD settings are a separate Running-VM operation. Never stops or reboots automatically. Requires ALLOW_DEBUGGER and confirm_vm.",
    )
    async def debug_serial_prepare(
        vm_name: str,
        pipe_name: str,
        confirm_vm: str,
    ) -> dict[str, Any]:
        return await _invoke(service.debug_serial_prepare, vm_name, pipe_name, confirm_vm)

    @tool(
        "debug_guest_prepare", annotations=MUTATION,
        description="Enable fixed COM1/115200 guest BCD debugging through PowerShell Direct while the VM is Running. "
        "Requires ALLOW_DEBUGGER, guest credentials, and confirm_vm. A deliberate later reboot is required.",
    )
    async def debug_guest_prepare(vm_name: str, confirm_vm: str) -> dict[str, Any]:
        return await _invoke(service.debug_guest_prepare, vm_name, confirm_vm)

    @tool(
        "debug_kdnet_status", annotations=READ_ONLY,
        description="Read the KDNET setup of a VM: prepared port, host address, switch, firewall rule, "
        "whether the key is in the Credential Manager, and whether kd is attached. Never returns the key.",
    )
    async def debug_kdnet_status(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.debug_kdnet_status, vm_name)

    @tool(
        "debug_kdnet_prepare", annotations=MUTATION,
        description="Prepare KDNET on the internal lab switch: attach the VM adapter, allocate a UDP port "
        "in 50000-50039, set the guest BCD debug settings over PowerShell Direct, add a host firewall rule "
        "for kd.exe on that port only, and store the generated key in the Windows Credential Manager. "
        "The key never appears in a result. Requires ALLOW_DEBUGGER and confirm_vm; reboot the guest afterwards.",
    )
    async def debug_kdnet_prepare(
        vm_name: str,
        confirm_vm: str,
        port: int | None = None,
        use_existing_adapter: bool = False,
    ) -> dict[str, Any]:
        return await _invoke(
            service.debug_kdnet_prepare, vm_name, confirm_vm, port, use_existing_adapter
        )

    @tool(
        "debug_kdnet_deprovision", annotations=MUTATION,
        description="Remove the KDNET firewall rule, turn guest BCD debugging off, delete the stored key and "
        "the local KDNET state. Detaches an attached kd session first. Requires ALLOW_DEBUGGER and confirm_vm.",
    )
    async def debug_kdnet_deprovision(vm_name: str, confirm_vm: str) -> dict[str, Any]:
        return await _invoke(service.debug_kdnet_deprovision, vm_name, confirm_vm)

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
        "kd_break", annotations=MUTATION,
        description="Break into a running target and wait for the kd prompt. Uses the console break-in first "
        "and falls back to vm_inject_nmi when the target does not stop (nmi=false disables the fallback). "
        "Requires ALLOW_DEBUGGER.",
    )
    async def kd_break(
        vm_name: str, nmi: bool | None = None, timeout_ms: int | None = None
    ) -> dict[str, Any]:
        return await _invoke(service.kd_break, vm_name, nmi, timeout_ms)

    @tool(
        "kd_go", annotations=MUTATION,
        description="Resume the target (g, gh, gn, gc, gu) and wait until it breaks again or the timeout "
        "expires. Requires ALLOW_DEBUGGER.",
    )
    async def kd_go(
        vm_name: str, command: str = "g", timeout_ms: int | None = None
    ) -> dict[str, Any]:
        return await _invoke(service.kd_go, vm_name, command, timeout_ms)

    @tool(
        "kd_command", annotations=MUTATION,
        description="Run one kd command on an attached session and return its output, delimited by a unique "
        "per-session marker rather than a timeout. Commands that resume the target belong in kd_go. "
        "Requires ALLOW_DEBUGGER.",
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
        "kd_set_breakpoint", annotations=MUTATION,
        description="Set a software breakpoint (bp) or deferred breakpoint (bu) with an optional command "
        "string, e.g. MyDriver!Init or nt!KeBugCheckEx. Requires ALLOW_DEBUGGER.",
    )
    async def kd_set_breakpoint(
        vm_name: str, target: str, deferred: bool = False, command: str | None = None
    ) -> dict[str, Any]:
        return await _invoke(service.kd_set_breakpoint, vm_name, target, deferred, command)

    @tool(
        "kd_clear_breakpoints", annotations=MUTATION,
        description="Clear every breakpoint in the attached session (bc *). Requires ALLOW_DEBUGGER.",
    )
    async def kd_clear_breakpoints(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.kd_clear_breakpoints, vm_name)

    @tool(
        "kd_break_on_load", annotations=MUTATION,
        description="Break (sxe ld:module) or stop breaking (sxd ld:module) when a driver loads, so a load "
        "can be caught before its entry point runs. Requires ALLOW_DEBUGGER.",
    )
    async def kd_break_on_load(
        vm_name: str, module: str, enabled: bool = True
    ) -> dict[str, Any]:
        return await _invoke(service.kd_break_on_load, vm_name, module, enabled)

    @tool(
        "kd_stack", annotations=READ_ONLY,
        description="Read the current call stack (k) of the broken target.",
    )
    async def kd_stack(vm_name: str, depth: int = 20) -> dict[str, Any]:
        return await _invoke(service.kd_stack, vm_name, depth)

    @tool(
        "kd_modules", annotations=READ_ONLY,
        description="List loaded modules with base addresses and symbols (lm) in the attached session.",
    )
    async def kd_modules(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.kd_modules, vm_name)

    @tool(
        "kd_analyze", annotations=READ_ONLY,
        description="Run !analyze -v on the attached session and return the parsed report: bugcheck code, "
        "parameters, blamed driver, bucket id, and top stack frames.",
    )
    async def kd_analyze(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.kd_analyze, vm_name)

    @tool(
        "kd_dump_open", annotations=READ_ONLY,
        description="Analyze a crash dump on the host with kd -z, running !analyze -v plus optional extra "
        "commands. The dump must live under the configured evidence root and end in .dmp.",
    )
    async def kd_dump_open(
        vm_name: str, dump_path: str, commands: list[str] | None = None
    ) -> dict[str, Any]:
        return await _invoke(service.kd_dump_open, vm_name, dump_path, commands)

    @mcp.tool(
        name="console_screenshot",
        description="PNG screenshot of the guest display from the Hyper-V console thumbnail: no "
        "VMConnect window or interactive session needed, and at scale 1 its pixels are the same "
        "coordinates console_mouse_move uses. source='vmconnect' captures an existing visible "
        "VMConnect window instead. Requires ALLOW_SCREENSHOTS and an allowlist.",
        annotations=READ_ONLY,
        structured_output=False,
    )
    async def console_screenshot(
        vm_name: str,
        source: Literal["thumbnail", "vmconnect"] = "thumbnail",
        region: Literal["window", "client"] = "window",
        max_width: int = 1600,
    ) -> CallToolResult:
        def perform() -> Any:
            return service.capture_screenshot(capture, vm_name, source, region, max_width)

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
    from .events import EVENTS, EventSink
    backend = EventSink(GuestSplitBackend(), EVENTS)
    create_server(HyperVService(backend, Settings.from_env())).run(transport="stdio")


if __name__ == "__main__":
    main()
