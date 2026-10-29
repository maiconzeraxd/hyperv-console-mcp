"""The console thumbnail, straight from the provider.

``GetVirtualSystemThumbnailImage`` answers RGB565 at exactly the size asked for,
which is what makes the pixels line up with the mouse coordinates the console
tools use. The payload can be padded per row, so the stride is inferred from the
length the same way the bridge infers it.
"""

from __future__ import annotations

import base64
from typing import Any

from .connection import Connection, WmiFailure
from .vm import VM_STATES, find_vm

MIN_SIZE = 1
MAX_SIZE = 16384
BYTES_PER_PIXEL = 2


def vm_thumbnail(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = find_vm(connection, str(payload.get("vm") or ""))
    width = _size(payload.get("width"), "width")
    height = _size(payload.get("height"), "height")
    response = connection.call(
        _service(connection),
        "GetVirtualSystemThumbnailImage",
        TargetSystem=vm["__PATH"],
        WidthPixels=width,
        HeightPixels=height,
    )
    name = str(vm.get("ElementName") or "")
    code = _int(response.get("ReturnValue"))
    if code != 0:
        raise WmiFailure(f"GetVirtualSystemThumbnailImage returned {code} for '{name}'.")
    data = bytes(_int(value) & 0xFF for value in response.get("ImageData") or ())
    minimum = width * height * BYTES_PER_PIXEL
    if len(data) < minimum:
        raise WmiFailure(f"The console thumbnail was shorter than {minimum} bytes.")
    stride = len(data) // height
    if stride < width * BYTES_PER_PIXEL:
        stride = width * BYTES_PER_PIXEL
    return {
        "vm": name,
        "state": VM_STATES.get(_int(vm.get("EnabledState")), "Other"),
        "width": width,
        "height": height,
        "format": "rgb565",
        "byte_length": len(data),
        "stride_bytes": stride,
        "data": base64.b64encode(data).decode("ascii"),
    }


def _service(connection: Connection) -> str:
    rows = connection.query("SELECT * FROM Msvm_VirtualSystemManagementService")
    if len(rows) != 1:
        raise WmiFailure(
            f"Expected one management service on this host; found {len(rows)}.", code="wmi_unavailable"
        )
    return str(rows[0]["__PATH"])


def _size(value: Any, field: str) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError) as exc:
        raise WmiFailure(f"Thumbnail {field} must be a whole number of pixels.", code="invalid_argument") from exc
    if size < MIN_SIZE or size > MAX_SIZE:
        raise WmiFailure(
            "Thumbnail width and height must be between 1 and 16384 pixels.",
            code="invalid_argument",
        )
    return size


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
