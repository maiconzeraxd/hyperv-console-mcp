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
from .backend import PowerShellBackend
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
    service = service or HyperVService(PowerShellBackend(), Settings.from_env())
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
        description="Run up to 32 ordered text, key-chord, click, move, scroll, wait, or Ctrl+Alt+Delete actions. Stops at the first failure.",
    )
    async def console_batch(vm_name: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
        return await _invoke(service.console_batch, vm_name, actions)

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

    @mcp.tool(
        name="console_screenshot",
        description="Experimental PNG capture of an existing visible VMConnect window in the MCP server's interactive session. "
        "It does not read the guest framebuffer. Requires ALLOW_SCREENSHOTS and an allowlist.",
        annotations=READ_ONLY,
        structured_output=False,
    )
    async def console_screenshot(
        vm_name: str, region: Literal["window", "client"] = "window", max_width: int = 1600
    ) -> CallToolResult:
        def perform() -> Any:
            return service.capture_screenshot(capture, vm_name, region, max_width)

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
    backend = EventSink(PowerShellBackend(), EVENTS)
    create_server(HyperVService(backend, Settings.from_env())).run(transport="stdio")


if __name__ == "__main__":
    main()
