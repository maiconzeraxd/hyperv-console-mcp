"""Experimental capture of an existing VMConnect window in the interactive session."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from .errors import CaptureError


@dataclass(frozen=True, slots=True)
class Screenshot:
    png: bytes
    metadata: dict[str, object]


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("width", wintypes.LONG),
        ("height", wintypes.LONG),
        ("planes", wintypes.WORD),
        ("bit_count", wintypes.WORD),
        ("compression", wintypes.DWORD),
        ("size_image", wintypes.DWORD),
        ("x_pels_per_meter", wintypes.LONG),
        ("y_pels_per_meter", wintypes.LONG),
        ("colors_used", wintypes.DWORD),
        ("colors_important", wintypes.DWORD),
    ]


class _RgbQuad(ctypes.Structure):
    _fields_ = [
        ("blue", wintypes.BYTE),
        ("green", wintypes.BYTE),
        ("red", wintypes.BYTE),
        ("reserved", wintypes.BYTE),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("header", _BitmapInfoHeader), ("colors", _RgbQuad * 1)]


class VMConnectCapture:
    """Uses desktop capture; Hyper-V has no public guest-framebuffer screenshot API."""

    def capture(self, vm_name: str, *, region: str = "window", max_width: int = 1600) -> Screenshot:
        if region not in {"window", "client"}:
            raise CaptureError("Screenshot region must be 'window' or 'client'.")
        if not 640 <= max_width <= 3840:
            raise CaptureError("Screenshot max_width must be between 640 and 3840.")
        try:
            from PIL import Image
        except ImportError as exc:
            raise CaptureError("Pillow is required for VMConnect capture.") from exc

        handle, title = self._find_window(vm_name)
        user32 = ctypes.windll.user32
        if user32.IsIconic(handle):
            raise CaptureError("The VMConnect window is minimized. Restore it before capture.")
        rectangle = _Rect()
        if region == "client":
            client = _Rect()
            if not user32.GetClientRect(handle, ctypes.byref(client)):
                raise CaptureError("Could not query the VMConnect client area.")
            rectangle.left = 0
            rectangle.top = 0
            rectangle.right = client.right
            rectangle.bottom = client.bottom
        elif not user32.GetWindowRect(handle, ctypes.byref(rectangle)):
            raise CaptureError("Could not query the VMConnect window bounds.")
        original_width = rectangle.right - rectangle.left
        original_height = rectangle.bottom - rectangle.top
        if original_width <= 0 or original_height <= 0:
            raise CaptureError("The VMConnect window has no visible capture area.")
        image = self._print_window(
            handle, original_width, original_height, region=region, image_class=Image
        )
        scale = min(1.0, max_width / original_width)
        if scale < 1.0:
            image = image.resize(
                (max_width, max(1, round(original_height * scale))),
                image.Resampling.LANCZOS,
            )
        output = BytesIO()
        image.save(output, format="PNG", compress_level=6)
        png = output.getvalue()
        if len(png) > 12 * 1024 * 1024:
            raise CaptureError("The captured PNG exceeds 12 MiB; retry with a smaller max_width.")
        metadata: dict[str, object] = {
            "vm": vm_name,
            "experimental": True,
            "source": "VMConnect PrintWindow capture (occlusion-safe when supported)",
            "window_title": title,
            "region": region,
            "width": image.width,
            "height": image.height,
            "source_width": original_width,
            "source_height": original_height,
            "capture_scale": round(scale, 6),
            "coordinate_note": (
                "Mouse tools use guest-display pixels, not screenshot pixels. "
                "Account for VMConnect controls, scaling, and letterboxing."
            ),
        }
        return Screenshot(png=png, metadata=metadata)

    @staticmethod
    def _print_window(
        handle: int, width: int, height: int, *, region: str, image_class: object
    ) -> object:
        """Ask the target window to render itself; never capture unrelated desktop pixels."""
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        handle_type = ctypes.c_void_p
        gdi32.CreateCompatibleDC.argtypes = [handle_type]
        gdi32.CreateCompatibleDC.restype = handle_type
        gdi32.CreateCompatibleBitmap.argtypes = [handle_type, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype = handle_type
        gdi32.SelectObject.argtypes = [handle_type, handle_type]
        gdi32.SelectObject.restype = handle_type
        gdi32.DeleteObject.argtypes = [handle_type]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.DeleteDC.argtypes = [handle_type]
        gdi32.DeleteDC.restype = wintypes.BOOL
        gdi32.GetDIBits.argtypes = [
            handle_type, handle_type, wintypes.UINT, wintypes.UINT,
            ctypes.c_void_p, ctypes.POINTER(_BitmapInfo), wintypes.UINT,
        ]
        gdi32.GetDIBits.restype = ctypes.c_int
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = handle_type
        user32.GetWindowDC.argtypes = [wintypes.HWND]
        user32.GetWindowDC.restype = handle_type
        user32.ReleaseDC.argtypes = [wintypes.HWND, handle_type]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.PrintWindow.argtypes = [wintypes.HWND, handle_type, wintypes.UINT]
        user32.PrintWindow.restype = wintypes.BOOL

        source_dc = (
            user32.GetWindowDC(handle) if region == "window" else user32.GetDC(handle)
        )
        if not source_dc:
            raise CaptureError("Could not acquire the VMConnect device context.")
        memory_dc = None
        bitmap = None
        previous = None
        try:
            memory_dc = gdi32.CreateCompatibleDC(source_dc)
            bitmap = gdi32.CreateCompatibleBitmap(source_dc, width, height)
            if not memory_dc or not bitmap:
                raise CaptureError("Could not allocate the VMConnect capture bitmap.")
            previous = gdi32.SelectObject(memory_dc, bitmap)
            # PW_RENDERFULLCONTENT=2: necessary for many DWM-composed windows.
            if not user32.PrintWindow(handle, memory_dc, 2):
                raise CaptureError(
                    "PrintWindow could not render VMConnect "
                    f"(Win32 error {ctypes.get_last_error()}). Restore and foreground its window "
                    "at the same elevation as the MCP server; no fallback will capture unrelated desktop content."
                )
            info = _BitmapInfo()
            info.header.size = ctypes.sizeof(_BitmapInfoHeader)
            info.header.width = width
            info.header.height = -height  # top-down DIB
            info.header.planes = 1
            info.header.bit_count = 32
            info.header.compression = 0
            buffer = ctypes.create_string_buffer(width * height * 4)
            if gdi32.GetDIBits(
                memory_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0
            ) != height:
                raise CaptureError("Could not read the rendered VMConnect bitmap.")
            return image_class.frombuffer(
                "RGBA", (width, height), buffer, "raw", "BGRA", 0, 1
            ).convert("RGB")
        finally:
            if previous and memory_dc:
                gdi32.SelectObject(memory_dc, previous)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if memory_dc:
                gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(handle, source_dc)

    @staticmethod
    def _find_window(vm_name: str) -> tuple[int, str]:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        try:
            # DPI awareness avoids virtualized and incorrect rectangles on mixed-DPI monitors.
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            pass

        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_Rect)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(_Rect)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(_Point)]
        user32.ClientToScreen.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

        prefix = vm_name.casefold() + " "
        matches: list[tuple[int, str]] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(handle: int, _: int) -> bool:
            if not user32.IsWindowVisible(handle):
                return True
            length = user32.GetWindowTextLengthW(handle)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(handle, buffer, len(buffer))
            title = buffer.value
            if not title.casefold().startswith(prefix):
                return True
            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
            process = kernel32.OpenProcess(0x1000, False, process_id.value)
            if not process:
                return True
            try:
                path_buffer = ctypes.create_unicode_buffer(32_768)
                path_length = wintypes.DWORD(len(path_buffer))
                if kernel32.QueryFullProcessImageNameW(
                    process, 0, path_buffer, ctypes.byref(path_length)
                ):
                    executable = Path(path_buffer.value).name.casefold()
                    if executable == "vmconnect.exe":
                        matches.append((int(handle), title))
            finally:
                kernel32.CloseHandle(process)
            return True

        callback = callback_type(visit)
        if not user32.EnumWindows(callback, 0):
            raise CaptureError("Could not enumerate interactive windows.")
        if not matches:
            raise CaptureError(
                f"No visible VMConnect window was found for {vm_name!r}. "
                f"Open it in the same interactive session: vmconnect.exe localhost '{vm_name}'."
            )
        if len(matches) != 1:
            raise CaptureError(f"Multiple VMConnect windows match {vm_name!r}; close the duplicates.")
        return matches[0]
