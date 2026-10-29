"""In-process structured operation events shared by MCP and the local dashboard."""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import ValidationError

_MAX_EVENTS = 1000
_MAX_MESSAGE = 512
_actor: ContextVar[str] = ContextVar("hyperv_event_actor", default="mcp")
_operation_depth: ContextVar[int] = ContextVar("hyperv_event_operation_depth", default=0)
_approval_granted: ContextVar[bool] = ContextVar("hyperv_approval_granted", default=False)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, maximum: int = _MAX_MESSAGE) -> str:
    return str(value or "")[:maximum]


@dataclass(frozen=True, slots=True)
class OperationEvent:
    event_id: str
    sequence: int
    operation: str
    phase: str
    actor: str
    started_utc: str
    finished_utc: str | None
    vm_name: str | None
    correlation_id: str
    progress: int | None
    error_code: str | None
    message: str


class EventBus:
    def __init__(self, directory: Path | None = None) -> None:
        self._condition = threading.Condition()
        self._events: deque[OperationEvent] = deque(maxlen=_MAX_EVENTS)
        self._sequence = 0
        self._directory = directory

    def snapshot(self, after: int = 0) -> dict[str, Any]:
        with self._condition:
            events = [asdict(event) for event in self._events if event.sequence > after]
            return {"events": events, "latest_sequence": self._sequence}

    def publish(
        self,
        operation: str,
        phase: str,
        *,
        vm_name: str | None = None,
        correlation_id: str,
        started_utc: str | None = None,
        finished_utc: str | None = None,
        progress: int | None = None,
        error_code: str | None = None,
        message: str = "",
    ) -> OperationEvent:
        with self._condition:
            self._sequence += 1
            event = OperationEvent(
                event_id=uuid.uuid4().hex,
                sequence=self._sequence,
                operation=_text(operation, 128),
                phase=phase,
                actor=_actor.get(),
                started_utc=started_utc or utc_now(),
                finished_utc=finished_utc,
                vm_name=_text(vm_name, 256) or None,
                correlation_id=correlation_id,
                progress=progress,
                error_code=_text(error_code, 128) or None,
                message=_text(message),
            )
            self._events.append(event)
            self._condition.notify_all()
        try:
            directory = self._directory or Settings.from_env().event_directory
            directory = directory.expanduser()
            directory.mkdir(parents=True, exist_ok=True)
            record = directory / f"{event.started_utc.replace(':', '-')}-{event.sequence:012d}-{event.event_id}.json"
            import json
            record.write_text(json.dumps(asdict(event), ensure_ascii=False), encoding="utf-8")
        except (OSError, ValidationError):
            pass
        return event

    def wait(self, after: int, timeout: float = 20) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._sequence <= after:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
        return self.snapshot(after)

    @contextmanager
    def actor(self, name: str) -> Iterator[None]:
        token = _actor.set(name)
        try:
            yield
        finally:
            _actor.reset(token)

    @contextmanager
    def operation(self, name: str, vm_name: str | None = None) -> Iterator[str]:
        depth = _operation_depth.get()
        token = _operation_depth.set(depth + 1)
        try:
            if depth:
                yield ""
                return
            correlation = uuid.uuid4().hex
            started = utc_now()
            self.publish(name, "started", vm_name=vm_name, correlation_id=correlation, started_utc=started)
            try:
                yield correlation
            except Exception as exc:
                self.publish(
                    name, "failed", vm_name=vm_name, correlation_id=correlation,
                    started_utc=started, finished_utc=utc_now(),
                    error_code=_text(getattr(exc, "code", type(exc).__name__), 128),
                    message="Operation failed; see the tool result for details.",
                )
                raise
            self.publish(
                name, "completed", vm_name=vm_name, correlation_id=correlation,
                started_utc=started, finished_utc=utc_now(), progress=100,
            )
        finally:
            _operation_depth.reset(token)


class ProgressReporter:
    def __init__(self, bus: EventBus, operation: str, vm_name: str, correlation_id: str) -> None:
        self.bus = bus
        self.operation = operation
        self.vm_name = vm_name
        self.correlation_id = correlation_id
        self.started_utc = utc_now()

    def update(self, progress: int, message: str) -> None:
        self.bus.publish(
            self.operation, "progress", vm_name=self.vm_name,
            correlation_id=self.correlation_id, started_utc=self.started_utc,
            progress=max(0, min(99, progress)), message=message,
        )


def _approval_required() -> bool:
    enabled = os.environ.get(
        "HYPERV_MCP_REQUIRE_DASHBOARD_APPROVAL", "false"
    ).strip().casefold() in {"1", "true", "yes", "on"}
    return enabled and _actor.get() != "dashboard"


def require_dashboard_approval(
    operation: str, vm_name: str | None, summary: dict[str, Any] | None = None
) -> None:
    """Ask the local dashboard before a mutation the tool did not already gate.

    Inside `approval_scope` the answer is inherited: a tool that already obtained
    one approval does not ask again for each file, chunk, or nested action.
    """
    if not _approval_required() or _approval_granted.get():
        return
    from .ipc import request_approval
    request_approval(operation, vm_name, summary)


@contextmanager
def approval_scope(
    operation: str, vm_name: str | None, summary: dict[str, Any] | None = None
) -> Iterator[None]:
    """Request exactly one dashboard approval for a whole tool call.

    Nested mutations (per-file writes, per-chunk assembly, batch console actions)
    run under the same approval instead of producing one prompt each.
    """
    if not _approval_required() or _approval_granted.get():
        yield
        return
    from .ipc import request_approval
    request_approval(operation, vm_name, summary)
    token = _approval_granted.set(True)
    try:
        yield
    finally:
        _approval_granted.reset(token)


class EventSink:
    """Optional bridge observer; credentials and file contents are never forwarded."""

    def __init__(self, backend: Any, bus: EventBus) -> None:
        self.backend = backend
        self.bus = bus

    def invoke(self, operation: str, payload: Any, *, timeout: float | None = None) -> dict[str, Any]:
        # Approval is requested by the service at tool level, never per bridge
        # operation, so one lab_stage does not ask once per file and chunk.
        vm_name = str(payload.get("vm", "")) or None
        with self.bus.operation(operation, vm_name):
            return self.backend.invoke(operation, payload, timeout=timeout)

    def close(self) -> None:
        self.backend.close()


EVENTS = EventBus()
