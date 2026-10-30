"""Synthetic console input, screenshots and the change poller."""

from __future__ import annotations

import base64
import binascii
import time
from typing import Any

from ..capture import (
    changed_fraction,
    decode_rgb565,
    sample_image,
    thumbnail_screenshot,
)
from ..errors import CaptureError, ValidationError
from ._base import (
    BUTTONS,
    KEY_NAMES,
    WAIT_CHANGE_THRESHOLD,
    WAIT_POLL_SECONDS,
    WAIT_SAMPLE_SIZE,
    WAIT_SAMPLE_WIDTH,
    ServiceBase,
    _ascii_keyboard_text,
    _integer,
    _text,
)


class ConsoleService(ServiceBase):
    def console_info(self, vm_name: str) -> dict[str, Any]:
        name = self._vm(vm_name)
        return self._run("console_info", "console_info", {"vm": name}, vm_name=name)

    def console_type_text(self, vm_name: str, text: str) -> dict[str, Any]:
        vm = self._write(vm_name, "console")
        value = _ascii_keyboard_text(_text(text, "text", 16_384, allow_empty=True))
        with self._approve("console_input", vm, {"characters": len(value)}):
            result = self._run(
                "console_type_text", "console_type_text", {"vm": vm, "text": value}, vm_name=vm
            )
        result["characters_requested"] = len(value)
        return result

    def console_press_keys(self, vm_name: str, keys: list[str]) -> dict[str, Any]:
        vm = self._write(vm_name, "console")
        if not isinstance(keys, list) or not 1 <= len(keys) <= 6:
            raise ValidationError("keys must contain between one and six key names.")
        if any(not isinstance(key, str) for key in keys):
            raise ValidationError("keys must contain only string key names.")
        normalized = [key.casefold() for key in keys]
        if any(key not in KEY_NAMES for key in normalized) or len(set(normalized)) != len(normalized):
            raise ValidationError("keys contains an unsupported or duplicate key name.")
        with self._approve("console_input", vm, {"keys": normalized}):
            return self._run(
                "console_press_keys", "console_press_keys", {"vm": vm, "keys": normalized}, vm_name=vm
            )

    def console_key_state(self, vm_name: str, key: str, down: bool) -> dict[str, Any]:
        vm = self._write(vm_name, "console")
        if not isinstance(key, str):
            raise ValidationError("key must be a string key name.")
        normalized = key.casefold()
        if normalized not in KEY_NAMES:
            raise ValidationError("key is not a supported key name.")
        if not isinstance(down, bool):
            raise ValidationError("down must be a boolean value.")
        with self._approve("console_input", vm, {"key": normalized, "down": down}):
            return self._run(
                "console_key_state", "console_key_state",
                {"vm": vm, "key": normalized, "down": down}, vm_name=vm,
            )

    def console_mouse_buttons(
        self,
        vm_name: str,
        left: bool = False,
        right: bool = False,
        middle: bool = False,
    ) -> dict[str, Any]:
        vm = self._write(vm_name, "console")
        if not all(isinstance(value, bool) for value in (left, right, middle)):
            raise ValidationError("Mouse button states must be boolean values.")
        with self._approve("console_input", vm, {
            "left": left, "right": right, "middle": middle,
        }):
            return self._run(
                "console_mouse_buttons", "console_mouse_buttons",
                {"vm": vm, "left": left, "right": right, "middle": middle}, vm_name=vm,
            )

    def console_batch(self, vm_name: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
        vm = self._write(vm_name, "console")
        if not isinstance(actions, list) or not 1 <= len(actions) <= 32:
            raise ValidationError("actions must contain between one and 32 console actions.")
        allowed_fields = {
            "text": {"text"}, "keys": {"keys"}, "click": {"button", "x", "y"},
            "move": {"x", "y"}, "scroll": {"delta"}, "wait": {"milliseconds"},
            "key_state": {"key", "down"},
            "mouse_buttons": {"left", "right", "middle"},
            "ctrl_alt_delete": set(),
        }
        for index, action in enumerate(actions):
            if not isinstance(action, dict) or not isinstance(action.get("type"), str):
                raise ValidationError(f"actions[{index}] must have a string type.")
            kind = action["type"]
            if kind not in allowed_fields:
                raise ValidationError(f"Unsupported console action type: {kind!r}.")
            unexpected = set(action) - {"type"} - allowed_fields[kind]
            if unexpected:
                raise ValidationError(f"actions[{index}] contains unsupported fields.")
        prepared: list[tuple[int, str, dict[str, Any]]] = []
        wait_budget = 0
        for index, action in enumerate(actions):
            kind = action["type"]
            arguments = {key: value for key, value in action.items() if key != "type"}
            if kind == "text":
                arguments["text"] = _ascii_keyboard_text(_text(
                    arguments.get("text", ""), f"actions[{index}].text", 16_384, allow_empty=True
                ))
            elif kind == "keys":
                keys = arguments.get("keys", [])
                if not isinstance(keys, list) or not 1 <= len(keys) <= 6 or any(
                    not isinstance(key, str) for key in keys
                ):
                    raise ValidationError(f"actions[{index}].keys must contain one to six strings.")
                normalized = [key.casefold() for key in keys]
                if any(key not in KEY_NAMES for key in normalized) or len(set(normalized)) != len(normalized):
                    raise ValidationError(f"actions[{index}].keys contains an unsupported or duplicate key.")
                arguments["keys"] = normalized
            elif kind in {"move", "click"}:
                x, y = arguments.get("x"), arguments.get("y")
                if kind == "move" or x is not None or y is not None:
                    if (x is None) != (y is None):
                        raise ValidationError(f"actions[{index}] x and y must be supplied together.")
                    arguments["x"] = _integer(x, f"actions[{index}].x", 0, 32_767)
                    arguments["y"] = _integer(y, f"actions[{index}].y", 0, 32_767)
                if kind == "click":
                    button = arguments.get("button", "left")
                    if button not in BUTTONS:
                        raise ValidationError(f"actions[{index}].button must be left, right, or middle.")
                    arguments["button_index"] = BUTTONS[button]
                    del arguments["button"]
            elif kind == "scroll":
                arguments["delta"] = _integer(
                    arguments.get("delta"), f"actions[{index}].delta", -100_000, 100_000
                )
            elif kind == "key_state":
                key = arguments.get("key")
                if not isinstance(key, str) or key.casefold() not in KEY_NAMES:
                    raise ValidationError(f"actions[{index}].key is not a supported key.")
                arguments["key"] = key.casefold()
                down = arguments.get("down")
                if not isinstance(down, bool):
                    raise ValidationError(f"actions[{index}].down must be a boolean value.")
            elif kind == "mouse_buttons":
                for field in ("left", "right", "middle"):
                    value = arguments.get(field, False)
                    if not isinstance(value, bool):
                        raise ValidationError(f"actions[{index}].{field} must be a boolean value.")
                    arguments[field] = value
            elif kind == "wait":
                duration = _integer(
                    arguments.get("milliseconds"), f"actions[{index}].milliseconds", 1, 30_000
                )
                wait_budget += duration
                if wait_budget > 30_000:
                    raise ValidationError("The cumulative console batch wait budget is 30000 ms.")
                arguments["milliseconds"] = duration
            prepared.append((index, kind, arguments))
        # One approval and one bridge request cover the whole ordered batch, so an
        # intermediate state cannot be interleaved with another console call.
        with self._approve("console_input", vm, {
            "actions": [kind for _, kind, _ in prepared],
            "action_count": len(prepared),
        }):
            payload = [
                {"type": kind, **arguments} for _, kind, arguments in prepared
            ]
            return self._run(
                "console_batch", "console_batch", {"vm": vm, "actions": payload}, vm_name=vm
            )

    def console_ctrl_alt_delete(self, vm_name: str) -> dict[str, Any]:
        vm = self._write(vm_name, "console")
        with self._approve("console_input", vm):
            return self._run(
                "console_ctrl_alt_delete", "console_ctrl_alt_delete", {"vm": vm}, vm_name=vm
            )

    def console_mouse_move(self, vm_name: str, x: int, y: int) -> dict[str, Any]:
        vm = self._write(vm_name, "console")
        payload = {
            "vm": vm,
            "x": _integer(x, "x", 0, 32_767),
            "y": _integer(y, "y", 0, 32_767),
        }
        with self._approve("console_input", vm, {"x": payload["x"], "y": payload["y"]}):
            return self._run("console_mouse_move", "console_mouse_move", payload, vm_name=vm)

    def console_click(
        self, vm_name: str, button: str = "left", x: int | None = None, y: int | None = None
    ) -> dict[str, Any]:
        vm = self._write(vm_name, "console")
        if button not in BUTTONS:
            raise ValidationError("button must be left, right, or middle.")
        if (x is None) != (y is None):
            raise ValidationError("x and y must be supplied together.")
        payload: dict[str, Any] = {"vm": vm, "button_index": BUTTONS[button]}
        if x is not None and y is not None:
            payload.update(
                x=_integer(x, "x", 0, 32_767), y=_integer(y, "y", 0, 32_767)
            )
        summary = {"button": button, "x": payload.get("x"), "y": payload.get("y")}
        with self._approve("console_input", vm, summary):
            return self._run("console_click", "console_click", payload, vm_name=vm)

    def console_scroll(self, vm_name: str, delta: int) -> dict[str, Any]:
        vm = self._write(vm_name, "console")
        amount = _integer(delta, "delta", -100_000, 100_000)
        with self._approve("console_input", vm, {"delta": amount}):
            return self._run(
                "console_scroll", "console_scroll",
                {"vm": vm, "delta": amount}, vm_name=vm,
            )

    def authorize_screenshot(self, vm_name: str) -> str:
        return self._write(vm_name, "screenshots")

    def _video_head(self, vm: str, *, audit: bool = True) -> tuple[int, int]:
        """The guest display resolution, which is the coordinate space mouse tools use."""
        info = self._run("console_info", "console_info", {"vm": vm}, vm_name=vm, audit=audit)
        for head in info.get("video_heads") or []:
            if not isinstance(head, dict):
                continue
            width, height = head.get("horizontal"), head.get("vertical")
            if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
                return width, height
        raise CaptureError(
            "The VM reported no video head with a current resolution. Start the guest and retry."
        )

    def _thumbnail_image(self, vm: str, width: int, height: int, *, audit: bool = True) -> Any:
        result = self._run(
            "vm_thumbnail", "vm_thumbnail",
            {"vm": vm, "width": width, "height": height}, vm_name=vm, audit=audit,
        )
        encoded = result.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise CaptureError("The bridge returned no console thumbnail payload.")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CaptureError("The console thumbnail payload was not valid base64.") from exc
        stride = result.get("stride_bytes")
        return decode_rgb565(
            payload,
            int(result.get("width") or width),
            int(result.get("height") or height),
            stride=stride if isinstance(stride, int) else None,
        )

    def capture_thumbnail(self, vm_name: str, max_width: int = 1600) -> Any:
        """Screenshot straight from the Hyper-V console thumbnail, with no VMConnect window."""
        name = self.authorize_screenshot(vm_name)
        width = _integer(max_width, "max_width", 640, 3840)
        with self._approve("console_screenshot", name, {"source": "thumbnail", "max_width": width}):
            head_width, head_height = self._video_head(name)
            image = self._thumbnail_image(name, head_width, head_height)
            return thumbnail_screenshot(
                image, vm=name, source_width=head_width, source_height=head_height,
                max_width=width,
            )

    def capture_screenshot(
        self, vm_name: str, source: str = "thumbnail",
        region: str = "window", max_width: int = 1600,
    ) -> Any:
        """The capture object lives here rather than with the caller.

        A remote client cannot pass a live window handle over the service
        boundary, and it should not: the desktop being captured is the host's.
        """
        if source == "thumbnail":
            return self.capture_thumbnail(vm_name, max_width)
        if source != "vmconnect":
            raise ValidationError("source must be 'thumbnail' or 'vmconnect'.")
        if region not in {"window", "client"}:
            raise ValidationError("region must be 'window' or 'client'.")
        name = self.authorize_screenshot(vm_name)
        with self._approve("console_screenshot", name, {"source": "vmconnect", "region": region}):
            return self._local_action(
                "console_screenshot", name,
                lambda: self.capture.capture(name, region=region, max_width=max_width),
            )

    def console_frame_size(self, vm_name: str) -> dict[str, Any]:
        """The guest display size, read once when a recording starts.

        The resolution cannot change between two frames of one driver run, so a
        recorder asks for it once and then reads only the thumbnail.
        """
        name = self.authorize_screenshot(vm_name)
        width, height = self._video_head(name)
        return {"vm": name, "width": width, "height": height}

    def console_frame(
        self, vm_name: str, width: int, height: int, max_width: int = 640
    ) -> dict[str, Any]:
        """One console frame as a PNG, for the recording a cycle writes.

        The same thumbnail read the console page uses and the same capability
        gate, but with no approval and no audit record per frame: the cycle that
        started the recording already passed both, and a ten-minute run would
        otherwise write three hundred audit entries for its pictures.
        """
        name = self._write(vm_name, "screenshots")
        size = _integer(max_width, "max_width", 640, 3840)
        image = self._thumbnail_image(name, int(width), int(height), audit=False)
        shot = thumbnail_screenshot(
            image, vm=name, source_width=int(width), source_height=int(height), max_width=size,
        )
        return {"vm": name, "png": shot.png, **shot.metadata}

    def console_wait_for_change(
        self, vm_name: str, timeout_ms: int = 30_000,
        region: list[int] | None = None, min_change: float = WAIT_CHANGE_THRESHOLD,
    ) -> dict[str, Any]:
        """Poll a small console thumbnail until the screen changes or the deadline passes."""
        name = self.authorize_screenshot(vm_name)
        timeout = _integer(timeout_ms, "timeout_ms", 250, 120_000)
        if isinstance(min_change, bool) or not isinstance(min_change, (int, float)) or not (
            0.0 <= float(min_change) <= 1.0
        ):
            raise ValidationError("min_change must be a number between 0 and 1.")
        box = self._region(region)
        head_width, head_height = self._video_head(name)
        sample_width = min(WAIT_SAMPLE_WIDTH, head_width)
        sample_height = max(1, round(head_height * sample_width / head_width))
        scaled = None
        if box is not None:
            x, y, width, height = box
            if x + width > head_width or y + height > head_height:
                raise ValidationError(
                    f"region exceeds the {head_width}x{head_height} guest display."
                )
            scaled = (
                round(x * sample_width / head_width),
                round(y * sample_height / head_height),
                max(1, round(width * sample_width / head_width)),
                max(1, round(height * sample_height / head_height)),
            )
        before = sample_image(
            self._thumbnail_image(name, sample_width, sample_height),
            region=scaled, size=WAIT_SAMPLE_SIZE,
        )
        started = time.monotonic()
        deadline = started + timeout / 1000
        polls = 0
        fraction = 0.0
        changed = False
        while True:
            # Always sample at least once, even when the timeout is shorter than a
            # poll interval, so a short wait still observes the screen.
            time.sleep(min(WAIT_POLL_SECONDS, max(0.01, deadline - time.monotonic())))
            polls += 1
            after = sample_image(
                self._thumbnail_image(name, sample_width, sample_height),
                region=scaled, size=WAIT_SAMPLE_SIZE,
            )
            fraction = changed_fraction(before, after)
            if fraction >= float(min_change):
                changed = True
                break
            if time.monotonic() >= deadline:
                break
        return {
            "vm": name,
            "changed": changed,
            "timed_out": not changed,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "polls": polls,
            "change_fraction": round(fraction, 4),
            "min_change": float(min_change),
            "timeout_ms": timeout,
            "region": list(box) if box is not None else None,
            "display": {"width": head_width, "height": head_height},
            "sample": {"width": sample_width, "height": sample_height},
        }

    @staticmethod
    def _region(region: Any) -> tuple[int, int, int, int] | None:
        if region is None:
            return None
        if not isinstance(region, (list, tuple)) or len(region) != 4:
            raise ValidationError("region must be [x, y, width, height] or omitted.")
        x, y, width, height = (
            _integer(value, f"region[{index}]", 0, 32_767)
            for index, value in enumerate(region)
        )
        if width < 1 or height < 1:
            raise ValidationError("region width and height must be at least 1.")
        return (x, y, width, height)
