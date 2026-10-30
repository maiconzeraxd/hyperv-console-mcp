"""A GIF of the console, taken while a cycle runs.

A receipt says what the run decided and what it left behind; it cannot show what
the screen did between the checkpoint and the crash. A driver test that hangs on
a splash screen and one that hangs on the desktop produce the same record, and
the difference is the picture.

The recorder samples the console thumbnail on its own thread, at a cadence a
person can watch rather than a frame rate, and stops with the cycle. A run
longer than the frame budget does not stop recording: it halves what it has kept
and doubles the spacing, so the file covers the whole run at a coarser rate and
always keeps the last frame - which is the one that answers "where did it stop".
The finished file is shrunk again on the way out until it fits a size budget: a
recording is evidence beside a receipt, not the reason the evidence folder is
large. Nothing here touches a VM beyond the same thumbnail read the console page
uses.
"""

from __future__ import annotations

import hashlib
import io
import threading
import time
from pathlib import Path
from typing import Any, Callable

# One frame every two seconds: a driver run is minutes long, and the picture
# that matters is which screen it was on when it stopped, not the cursor.
INTERVAL_SECONDS = 2.0
# Sixty kept frames. Past that the recorder halves the set rather than stopping,
# so the recording always ends where the run ended.
MAX_FRAMES = 60
# Eight megabytes: big enough to show a desktop, small enough to mail.
MAX_BYTES = 8 * 1024 * 1024
FILE_NAME = "console.gif"
MAX_ERROR_CHARS = 300


def _gif(frames: list[Any], duration_ms: int) -> bytes:
    buffer = io.BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=duration_ms,
        loop=0,
        optimize=True,
    )
    return buffer.getvalue()


def _thinned(frames: list[Any], stride: int) -> list[Any]:
    """Every ``stride``-th frame, plus the last one: a recording keeps its end."""
    if stride <= 1:
        return list(frames)
    kept = frames[::stride]
    if frames[-1] is not kept[-1]:
        kept.append(frames[-1])
    return kept


class Recorder:
    """Samples a callable that returns PNG bytes, and writes them as one GIF.

    ``capture`` is a plain callable rather than the service so this module can be
    tested with an image function and no VM, and so the caller decides whether a
    frame is read with or without an approval of its own.
    """

    def __init__(
        self,
        capture: Callable[[], bytes | None],
        *,
        interval: float = INTERVAL_SECONDS,
        max_frames: int = MAX_FRAMES,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self.capture = capture
        self.interval = max(0.2, float(interval))
        self.max_frames = max(2, int(max_frames))
        self.max_bytes = max(1024, int(max_bytes))
        self.frames: list[Any] = []
        self.failures = 0
        # How many samples are skipped between kept frames. It doubles whenever
        # the kept set fills up, so a long run stays covered end to end.
        self.stride = 1
        self.samples = 0
        self.started: float | None = None
        self.stopped: float | None = None
        self._ticks = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Take the first frame now, then one every interval on a thread."""
        self.started = time.monotonic()
        self._sample()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="hvmcp-record")
        self._thread.start()

    def stop(self, directory: Path) -> dict[str, Any]:
        """Stop sampling and write the file. Never raises: a cycle outranks a GIF."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5.0)
        self.stopped = time.monotonic()
        if not self.frames:
            return {
                "written": False,
                "reason": "no frame could be captured" if self.failures else "the run was too short",
                "failures": self.failures,
            }
        try:
            return self._write(Path(directory))
        except Exception as exc:  # noqa: BLE001 — evidence is never worth a failed run
            return {"written": False, "error": str(exc)[:MAX_ERROR_CHARS]}

    # -- internals ---------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self._ticks += 1
            if self._ticks % self.stride:
                continue
            self._sample()
            if len(self.frames) > self.max_frames:
                # Keep every other frame and sample half as often from here on,
                # so the budget bounds the file and not the length it covers.
                self.frames = _thinned(self.frames, 2)
                self.stride *= 2

    def _sample(self) -> None:
        self.samples += 1
        try:
            data = self.capture()
        except Exception:  # noqa: BLE001 — a frame that failed is one frame fewer
            self.failures += 1
            return
        if not data:
            self.failures += 1
            return
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                # Palette frames, not true colour: a GIF cannot hold more, and
                # converting once here is cheaper than letting the encoder pick.
                self.frames.append(image.convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE))
        except Exception:  # noqa: BLE001
            self.failures += 1

    def _write(self, directory: Path) -> dict[str, Any]:
        frames = self.frames
        # One frame stands for ``stride`` samples, so the GIF plays back over the
        # run's wall time however many times the set was halved.
        factor = 1
        duration_ms = int(self.interval * self.stride * 1000)
        data = _gif(frames, duration_ms)
        while len(data) > self.max_bytes and len(frames) > 2:
            factor *= 2
            frames = _thinned(self.frames, factor)
            duration_ms = int(self.interval * self.stride * factor * 1000)
            data = _gif(frames, duration_ms)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / FILE_NAME
        path.write_bytes(data)
        return {
            "written": True,
            "path": FILE_NAME,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "frames": len(frames),
            "sampled": self.samples,
            "duration_seconds": round((self.stopped or 0) - (self.started or 0), 1),
            "interval_seconds": round(self.interval * self.stride * factor, 1),
            "truncated": self.stride > 1 or factor > 1,
            "failures": self.failures,
        }


__all__ = [
    "FILE_NAME", "INTERVAL_SECONDS", "MAX_BYTES", "MAX_FRAMES", "Recorder",
]
