"""RGB565 thumbnail decoding and screen-change sampling, with no Hyper-V involved."""

from __future__ import annotations

import pytest

from hyperv_console_mcp.capture import (
    changed_fraction,
    decode_rgb565,
    encode_png,
    sample_image,
    thumbnail_screenshot,
)
from hyperv_console_mcp.errors import CaptureError

pytest.importorskip("PIL")


def pixel(red: int, green: int, blue: int) -> bytes:
    """Encode one RGB888 colour the way Hyper-V's console thumbnail stores it."""
    value = ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)
    return value.to_bytes(2, "little")


def test_rgb565_is_decoded_with_red_in_the_high_bits() -> None:
    data = pixel(255, 0, 0) + pixel(0, 255, 0) + pixel(0, 0, 255) + pixel(255, 255, 255)
    image = decode_rgb565(data, 2, 2)

    assert image.size == (2, 2)
    assert image.mode == "RGB"
    # Pillow scales the 5/6-bit channels to the full 0-255 range, so the extremes
    # are exact and red stays in the high bits rather than being read as blue.
    assert image.getpixel((0, 0)) == (255, 0, 0)
    assert image.getpixel((1, 0)) == (0, 255, 0)
    assert image.getpixel((0, 1)) == (0, 0, 255)
    assert image.getpixel((1, 1)) == (255, 255, 255)


def test_padded_rows_use_the_stride_the_payload_implies() -> None:
    padding = b"\x00" * 4
    data = pixel(255, 0, 0) + pixel(0, 255, 0) + padding
    data += pixel(0, 0, 255) + pixel(255, 255, 255) + padding

    image = decode_rgb565(data, 2, 2)

    assert image.getpixel((0, 0)) == (255, 0, 0)
    assert image.getpixel((1, 0)) == (0, 255, 0)
    assert image.getpixel((0, 1)) == (0, 0, 255)
    assert image.getpixel((1, 1)) == (255, 255, 255)


def test_a_short_payload_is_refused_rather_than_cropped() -> None:
    with pytest.raises(CaptureError, match="too few"):
        decode_rgb565(pixel(255, 0, 0), 4, 4)


def test_a_thumbnail_at_head_resolution_matches_mouse_coordinates() -> None:
    image = decode_rgb565(pixel(255, 0, 0) * 4, 2, 2)

    shot = thumbnail_screenshot(
        image, vm="NORTH-Test", source_width=2, source_height=2, max_width=1600
    )

    assert shot.png.startswith(b"\x89PNG")
    assert shot.metadata["coordinates_match_mouse"] is True
    assert shot.metadata["capture_scale"] == 1.0
    assert shot.metadata["source"] == "hyper-v console thumbnail"
    assert shot.metadata["width"] == 2


def test_a_downscaled_thumbnail_reports_the_scale_instead_of_lying() -> None:
    image = decode_rgb565(pixel(0, 0, 255) * (800 * 450), 800, 450)

    shot = thumbnail_screenshot(
        image, vm="NORTH-Test", source_width=800, source_height=450, max_width=640
    )

    assert shot.metadata["coordinates_match_mouse"] is False
    assert shot.metadata["width"] == 640
    assert shot.metadata["height"] == 360
    assert shot.metadata["capture_scale"] == pytest.approx(0.8)


def test_encode_png_refuses_an_unsupported_width() -> None:
    image = decode_rgb565(pixel(255, 255, 255) * 4, 2, 2)

    with pytest.raises(CaptureError, match="max_width"):
        encode_png(image, max_width=320)


def test_identical_frames_do_not_count_as_a_change() -> None:
    frame = decode_rgb565(pixel(10, 20, 30) * (16 * 9), 16, 9)

    first = sample_image(frame)
    second = sample_image(decode_rgb565(pixel(10, 20, 30) * (16 * 9), 16, 9))

    assert changed_fraction(first, second) == 0.0


def test_a_changed_region_is_measured_as_a_fraction() -> None:
    dark = sample_image(decode_rgb565(pixel(0, 0, 0) * (16 * 9), 16, 9))
    bright = sample_image(decode_rgb565(pixel(255, 255, 255) * (16 * 9), 16, 9))

    assert changed_fraction(dark, bright) == 1.0
    # Cropping the same image to its unchanged corner sees no change at all.
    frame = decode_rgb565(pixel(255, 255, 255) * (16 * 9), 16, 9)
    corner = sample_image(frame, region=(0, 0, 4, 2))
    assert changed_fraction(corner, corner) == 0.0


def test_samples_of_different_sizes_are_never_equal() -> None:
    assert changed_fraction(b"", b"") == 1.0
    assert changed_fraction(b"\x00", b"\x00\x00") == 1.0
