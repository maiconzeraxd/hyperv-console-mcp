"""MCP stdio server. No protocol data or diagnostics are written to stdout."""

from __future__ import annotations

import json
import logging
import sys
from functools import partial
from typing import Any, Callable, TypeVar

import anyio
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

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
        version="0.1.0",
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
        "vm_power", annotations=MUTATION,
        description="Start, gracefully shut down, save, forcibly turn off, or reset a VM. "
        "turn_off/reset require confirm_vm equal to vm_name. Requires ALLOW_POWER and an allowlist.",
    )
    async def vm_power(vm_name: str, action: str, confirm_vm: str | None = None) -> dict[str, Any]:
        return await _invoke(service.power, vm_name, action, confirm_vm)

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
        "guest_exec", annotations=MUTATION,
        description="Run PowerShell in the guest through PowerShell Direct. Credentials come only from the server environment. "
        "Returns combined output, exit code, and truncation status. A host-side timeout may leave the guest command running.",
    )
    async def guest_exec(vm_name: str, command: str) -> dict[str, Any]:
        return await _invoke(service.guest_exec, vm_name, command)

    @tool(
        "guest_read_file", annotations=READ_ONLY,
        description="Read an absolute guest file path as base64, with SHA-256. Default limit 1 MiB; maximum 8 MiB.",
    )
    async def guest_read_file(
        vm_name: str, guest_path: str, max_bytes: int = 1_048_576
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
        "console_info", annotations=READ_ONLY,
        description="Read guest video-head resolutions for mouse coordinate planning.",
    )
    async def console_info(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.console_info, vm_name)

    @tool(
        "console_type_text", annotations=MUTATION,
        description="Type text through Msvm_Keyboard, including newlines. No VMConnect focus is required. Requires ALLOW_CONSOLE.",
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
        vm_name: str, button: str = "left", x: int | None = None, y: int | None = None
    ) -> dict[str, Any]:
        return await _invoke(service.console_click, vm_name, button, x, y)

    @tool(
        "console_scroll", annotations=MUTATION,
        description="Apply a relative synthetic-mouse wheel delta. Positive values normally scroll up.",
    )
    async def console_scroll(vm_name: str, delta: int) -> dict[str, Any]:
        return await _invoke(service.console_scroll, vm_name, delta)

    @tool(
        "debug_serial_status", annotations=READ_ONLY,
        description="Read Hyper-V COM port debug configuration and local WinDbg/KDNET availability.",
    )
    async def debug_serial_status(vm_name: str) -> dict[str, Any]:
        return await _invoke(service.debug_serial_status, vm_name)

    @tool(
        "debug_serial_prepare", annotations=MUTATION,
        description="Prepare COM1 kernel debugging over a local named pipe. VM must already be Off; it is never stopped automatically. "
        "Optionally enable guest serial debugging with fixed bcdedit commands; reboot remains manual. Requires ALLOW_DEBUGGER and confirm_vm.",
    )
    async def debug_serial_prepare(
        vm_name: str,
        pipe_name: str,
        confirm_vm: str,
        configure_guest: bool = False,
    ) -> dict[str, Any]:
        return await _invoke(
            service.debug_serial_prepare, vm_name, pipe_name, confirm_vm, configure_guest
        )

    @mcp.tool(
        name="console_screenshot",
        description="Experimental PNG capture of an existing visible VMConnect window in the MCP server's interactive session. "
        "It does not read the guest framebuffer. Requires ALLOW_SCREENSHOTS and an allowlist.",
        annotations=READ_ONLY,
        structured_output=False,
    )
    async def console_screenshot(
        vm_name: str, region: str = "window", max_width: int = 1600
    ) -> CallToolResult:
        def perform() -> Any:
            allowed_name = service.authorize_screenshot(vm_name)
            return capture.capture(allowed_name, region=region, max_width=max_width)

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
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
