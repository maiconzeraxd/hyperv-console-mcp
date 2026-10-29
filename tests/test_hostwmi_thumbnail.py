"""The console thumbnail: RGB565 bytes, base64, and the stride the bridge infers."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from hyperv_console_mcp.hostwmi import WmiBackend
from hyperv_console_mcp.hostwmi.connection import WmiFailure
from hyperv_console_mcp.hostwmi.thumbnail import vm_thumbnail

VM_ID = "D5AD7D1D-5CB3-441F-A9D7-138640C8F61A"
MANAGEMENT = (
    r"\\HOST\root\virtualization\v2:Msvm_VirtualSystemManagementService."
    'CreationClassName="Msvm_VirtualSystemManagementService",Name="vmms",'
    'SystemCreationClassName="Msvm_ComputerSystem",SystemName="HOST"'
)


class FakeConnection:
    def __init__(self, response: dict[str, Any] | None = None, *, enabled_state: int = 2) -> None:
        self.response = {"ReturnValue": 0} if response is None else response
        self.enabled_state = enabled_state
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def query(self, wql: str, *, namespace: str = "") -> list[dict[str, Any]]:
        if "FROM Msvm_ComputerSystem" in wql:
            return [
                {
                    "ElementName": "NORTH-Test",
                    "Name": VM_ID,
                    "EnabledState": self.enabled_state,
                    "StatusDescriptions": [],
                    "OnTimeInMilliseconds": 0,
                    "__PATH": f'\\\\HOST\\root\\virtualization\\v2:Msvm_ComputerSystem.Name="{VM_ID}"',
                }
            ]
        if "FROM Msvm_VirtualSystemManagementService" in wql:
            return [{"__PATH": MANAGEMENT}]
        raise AssertionError(f"unexpected query: {wql}")

    def get(self, path: str, *, namespace: str = "") -> dict[str, Any]:
        raise AssertionError("the thumbnail needs no job")

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        self.calls.append((path, method, parameters))
        return self.response


def _image(width: int, height: int, stride: int | None = None) -> list[int]:
    """A payload whose first pixel of every row encodes the row number."""
    stride = stride or width * 2
    data: list[int] = []
    for row in range(height):
        line = [row, row] + [0] * (width * 2 - 2)
        data.extend(line + [0xEE] * (stride - width * 2))
    return data


def test_the_thumbnail_is_the_bridge_shape() -> None:
    connection = FakeConnection({"ReturnValue": 0, "ImageData": tuple(_image(4, 2))})
    result = vm_thumbnail(connection, {"vm": "NORTH-Test", "width": 4, "height": 2})
    assert result["vm"] == "NORTH-Test"
    assert result["state"] == "Running"
    assert (result["width"], result["height"]) == (4, 2)
    assert result["format"] == "rgb565"
    assert result["byte_length"] == 16
    assert result["stride_bytes"] == 8
    assert base64.b64decode(result["data"]) == bytes(_image(4, 2))


def test_the_provider_is_asked_for_the_size_the_caller_wanted() -> None:
    connection = FakeConnection({"ReturnValue": 0, "ImageData": tuple(_image(4, 2))})
    vm_thumbnail(connection, {"vm": "NORTH-Test", "width": 4, "height": 2})
    path, method, parameters = connection.calls[0]
    assert (path, method) == (MANAGEMENT, "GetVirtualSystemThumbnailImage")
    assert parameters["WidthPixels"] == 4
    assert parameters["HeightPixels"] == 2
    assert parameters["TargetSystem"].endswith(f'Name="{VM_ID}"')


def test_a_padded_payload_reports_the_stride_it_implies() -> None:
    connection = FakeConnection({"ReturnValue": 0, "ImageData": tuple(_image(4, 2, stride=12))})
    result = vm_thumbnail(connection, {"vm": "NORTH-Test", "width": 4, "height": 2})
    assert result["byte_length"] == 24
    assert result["stride_bytes"] == 12


def test_a_stopped_vm_reports_its_state() -> None:
    connection = FakeConnection({"ReturnValue": 0, "ImageData": tuple(_image(4, 2))}, enabled_state=3)
    assert vm_thumbnail(connection, {"vm": "NORTH-Test", "width": 4, "height": 2})["state"] == "Off"


def test_a_refused_call_keeps_the_bridge_wording() -> None:
    connection = FakeConnection({"ReturnValue": 4})
    with pytest.raises(WmiFailure) as caught:
        vm_thumbnail(connection, {"vm": "NORTH-Test", "width": 4, "height": 2})
    assert str(caught.value) == "GetVirtualSystemThumbnailImage returned 4 for 'NORTH-Test'."


def test_a_short_payload_is_an_error_not_a_cropped_image() -> None:
    connection = FakeConnection({"ReturnValue": 0, "ImageData": tuple(_image(4, 2)[:10])})
    with pytest.raises(WmiFailure) as caught:
        vm_thumbnail(connection, {"vm": "NORTH-Test", "width": 4, "height": 2})
    assert str(caught.value) == "The console thumbnail was shorter than 16 bytes."


@pytest.mark.parametrize(("width", "height"), [(0, 480), (1920, 0), (20000, 480), (1920, 20000)])
def test_a_size_outside_the_provider_range_is_refused_before_the_call(width: int, height: int) -> None:
    connection = FakeConnection({"ReturnValue": 0, "ImageData": tuple(_image(4, 2))})
    with pytest.raises(WmiFailure) as caught:
        vm_thumbnail(connection, {"vm": "NORTH-Test", "width": width, "height": height})
    assert caught.value.code == "invalid_argument"
    assert connection.calls == []


def test_a_size_that_is_not_a_number_is_refused() -> None:
    connection = FakeConnection({"ReturnValue": 0, "ImageData": tuple(_image(4, 2))})
    with pytest.raises(WmiFailure) as caught:
        vm_thumbnail(connection, {"vm": "NORTH-Test", "width": "wide", "height": 480})
    assert caught.value.code == "invalid_argument"


def test_the_backend_serves_vm_thumbnail() -> None:
    backend = WmiBackend(connect=lambda: FakeConnection({"ReturnValue": 0, "ImageData": tuple(_image(4, 2))}))
    result = backend.invoke("vm_thumbnail", {"vm": "NORTH-Test", "width": 4, "height": 2})
    assert result["byte_length"] == 16
