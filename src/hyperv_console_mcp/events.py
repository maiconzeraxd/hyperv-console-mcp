"""In-process structured operation events shared by MCP and the local dashboard."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import ValidationError

_MAX_EVENTS = 1000
_MAX_MESSAGE = 512
_actor: ContextVar[str] = ContextVar("hyperv_event_actor", default="mcp")
_operation_depth: ContextVar[int] = ContextVar("hyperv_event_operation_depth", default=0)
_approval_granted: ContextVar[bool] = ContextVar("hyperv_approval_granted", default=False)
_write_lock = threading.Lock()
_pruned: set[str] = set()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _day(stamp: str) -> str:
    """Return the YYYYMMDD bucket of an ISO-8601 UTC timestamp."""
    compact = stamp[:10].replace("-", "")
    return compact if len(compact) == 8 and compact.isdigit() else datetime.now(timezone.utc).strftime("%Y%m%d")


def _log_path(directory: Path, day: str) -> Path:
    return directory / f"events-{day}.jsonl"


def _append(directory: Path, day: str, record: str, retention_days: int) -> None:
    """Append one JSON line under an exclusive lock shared by MCP and dashboard."""
    directory = directory.expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        with _log_path(directory, day).open("a", encoding="utf-8", newline="") as stream:
            stream.write(record + "\n")
        key = str(directory)
        if key not in _pruned:
            _pruned.add(key)
            _prune(directory, retention_days)


def _prune(directory: Path, retention_days: int) -> None:
    # File names sort by day, so a string comparison is enough to find stale logs.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y%m%d")
    for path in directory.glob("events-*.jsonl"):
        if path.stem[len("events-"):] >= cutoff:
            continue
        try:
            path.unlink()
        except OSError:
            pass


class EventLogReader:
    """Reads the daily JSONL logs incrementally, resuming from a byte offset.

    The dashboard polls every 1.5 s; re-reading and re-sorting every file each
    time made that poll grow with the whole history.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._offsets: dict[Path, int] = {}

    def read_new(self, limit: int = 1000) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            paths = sorted(self.directory.expanduser().glob("events-*.jsonl"))
        except OSError:
            return records
        for path in paths:
            offset = self._offsets.get(path, 0)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < offset:  # the log was rotated or truncated under us
                offset = 0
            if size == offset:
                self._offsets[path] = offset
                continue
            try:
                with path.open("rb") as stream:
                    stream.seek(offset)
                    data = stream.read()
            except OSError:
                continue
            end = data.rfind(b"\n")
            if end < 0:
                continue  # only a partial line so far; wait for the next write
            complete = data[: end + 1]
            self._offsets[path] = offset + len(complete)
            for line in complete.splitlines():
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
        return records[-limit:]


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
            settings = Settings.from_env()
            directory = self._directory or settings.event_directory
            _append(
                directory, _day(event.started_utc),
                json.dumps(asdict(event), ensure_ascii=False),
                settings.event_retention_days,
            )
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
    # The lab profile exists so an autonomous loop does not wait for a click on
    # every step. It records every operation instead, and the allowlist and
    # every confirm_* field still apply.
    if os.environ.get("HYPERV_MCP_PROFILE", "").strip().casefold() == "lab":
        return False
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
