"""The console recording: a GIF of the run, bounded in frames and in bytes.

A recording is evidence, so the two things it must never do are fail the cycle
that produced it and grow without limit. These cover the budget (a long run is
thinned, not cut short), the failure paths (a frame that cannot be read is one
frame fewer), and the file itself (a real GIF that opens and holds the frames
the report says it does).
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any

from PIL import Image

from hyperv_console_mcp.lab import recording


def png(colour: tuple[int, int, int], size: tuple[int, int] = (16, 12)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def frames_in(path: Path) -> int:
    with Image.open(path) as image:
        return int(getattr(image, "n_frames", 1))


def test_the_recorder_writes_one_gif_of_the_frames_it_kept(tmp_path: Path) -> None:
    colours = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
    index = {"n": 0}

    def capture() -> bytes:
        colour = colours[index["n"] % len(colours)]
        index["n"] += 1
        return png(colour)

    recorder = recording.Recorder(capture, interval=0.2)
    recorder.start()
    time.sleep(0.75)
    result = recorder.stop(tmp_path)

    assert result["written"] is True, result
    assert result["path"] == recording.FILE_NAME
    path = tmp_path / recording.FILE_NAME
    assert path.is_file()
    assert result["size"] == path.stat().st_size
    assert frames_in(path) == result["frames"]
    assert result["frames"] >= 2
    assert result["sampled"] >= result["frames"]
    assert result["failures"] == 0
    assert len(result["sha256"]) == 64


def test_a_long_run_is_thinned_rather_than_cut_short(tmp_path: Path) -> None:
    """The frame budget bounds the file, not the length of run it covers."""
    colour = {"n": 0}

    def capture() -> bytes:
        colour["n"] += 1
        return png((colour["n"] % 256, 0, 0))

    recorder = recording.Recorder(capture, interval=0.2, max_frames=3)
    recorder.start()
    time.sleep(1.5)
    result = recorder.stop(tmp_path)

    assert recorder.stride > 1, "the kept set should have been halved"
    assert result["truncated"] is True
    # Thinning keeps the last frame, which is the screen the run stopped on.
    assert recorder.frames[-1] is not None
    # The effective spacing grew, so the GIF still plays over the run's wall time.
    assert result["interval_seconds"] > 0.2


def test_the_file_is_shrunk_until_it_fits_the_budget(tmp_path: Path) -> None:
    """Noisy frames do not fit; the writer drops frames until they do."""
    import os

    def capture() -> bytes:
        return png_bytes(os.urandom(64 * 64 * 3))

    recorder = recording.Recorder(capture, interval=0.2, max_bytes=1_500)
    recorder.start()
    time.sleep(1.1)
    sampled = len(recorder.frames)
    result = recorder.stop(tmp_path)

    assert result["written"] is True, result
    if sampled > 2:
        assert result["frames"] < sampled
        assert result["truncated"] is True


def png_bytes(raw: bytes) -> bytes:
    buffer = io.BytesIO()
    Image.frombytes("RGB", (64, 64), raw).save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_frame_that_cannot_be_read_is_counted_not_raised(tmp_path: Path) -> None:
    """A guest that stops answering costs frames, never the cycle's answer."""

    def capture() -> Any:
        raise RuntimeError("the console is gone")

    recorder = recording.Recorder(capture, interval=0.2)
    recorder.start()
    time.sleep(0.5)
    result = recorder.stop(tmp_path)

    assert result["written"] is False
    assert result["reason"] == "no frame could be captured"
    assert result["failures"] >= 1


def test_a_run_with_no_frames_says_which_kind_of_empty_it_was(tmp_path: Path) -> None:
    recorder = recording.Recorder(lambda: None, interval=0.2)
    recorder.started = time.monotonic()
    result = recorder.stop(tmp_path)

    assert result["written"] is False
    assert result["reason"] == "the run was too short"
    assert result["failures"] == 0


def test_the_gif_keeps_the_first_and_the_last_frame() -> None:
    """``_thinned`` is what makes the recording end where the run ended."""
    frames = [object() for _ in range(9)]
    kept = recording._thinned(frames, 4)

    assert kept[0] is frames[0]
    assert kept[-1] is frames[-1]
    assert len(kept) < len(frames)


def test_a_stride_of_one_keeps_every_frame() -> None:
    frames = [object() for _ in range(4)]
    assert recording._thinned(frames, 1) == frames
