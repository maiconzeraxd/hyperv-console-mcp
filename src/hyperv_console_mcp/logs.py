"""One structured log stream: what each subsystem said, and where to resume.

The event bus records what an operation *was* — started, completed, failed. It
deliberately carries no detail, because the detail lives in the subsystems:
the PowerShell worker, the debugger, the serial reader, the guest transport,
the host's WMI calls. Before this module each of them wrote wherever it liked,
and a window that wanted to show the work had to guess.

A line is redacted before it is stored, because the same stream feeds a dock an
operator watches and a client an AI reads. Readers resume from an offset
instead of re-reading the buffer, and a reader never holds the writer: a
subscription that waits 30 seconds must not stop the service from working.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .errors import ValidationError

# The subsystems a line can come from, named after what a person would call
# them. A filter over an open set filters nothing, so an unknown source is
# refused rather than stored.
SOURCES = frozenset({"worker", "kd", "serial", "ssh", "wmi", "lab", "service"})
# Ordered from quiet to loud: a level is a floor, not an equality.
LEVELS = ("debug", "info", "warning", "error")
_LEVEL_INDEX = {name: index for index, name in enumerate(LEVELS)}
_MAX_RECORDS = 2000
_MAX_MESSAGE = 512
_DEFAULT_LIMIT = 500

_correlation: ContextVar[str | None] = ContextVar("hyperv_log_correlation", default=None)

# `password=…`, `token: …`, `api_key = …`: the shapes a secret is written in
# when somebody means well and puts one in a message.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|senha|token|secret|api[_-]?key|key)\b\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)"
)
_BLOB = re.compile(r"[A-Za-z0-9+/=]{40,}")


def _mask_blob(match: re.Match[str]) -> str:
    blob = match.group(0)
    # A hex digest is a fact worth keeping; a base64 key is not. The two are
    # told apart by the alphabet the blob actually uses.
    if not any(character in blob for character in "+/="):
        return blob
    return "<redacted>"


def redact(text: str) -> str:
    """Mask the secret-shaped parts of a message, and nothing else."""
    masked = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return _BLOB.sub(_mask_blob, masked)


def _clip(value: Any, maximum: int = _MAX_MESSAGE) -> str:
    return str(value or "")[:maximum]


def _source(value: str) -> str:
    name = str(value or "").strip().casefold()
    if name not in SOURCES:
        raise ValidationError(
            f"Unknown log source {value!r}. Known: {', '.join(sorted(SOURCES))}."
        )
    return name


def _level(value: str) -> str:
    name = str(value or "").strip().casefold()
    if name not in _LEVEL_INDEX:
        raise ValidationError(
            f"Unknown log level {value!r}. Known: {', '.join(LEVELS)}."
        )
    return name


def source_for(operation: str) -> str:
    """Which subsystem an operation name belongs to.

    One mapping, used by the operation scope and by anything that logs about an
    operation, so a dock's per-source filter means the same thing everywhere.
    """
    name = str(operation or "").casefold()
    if name.startswith(("guest_ssh", "ssh_")):
        return "ssh"
    if name.startswith("kd_") or name == "vm_inject_nmi":
        return "kd"
    if name.startswith("serial_"):
        return "serial"
    if name.startswith("lab_"):
        return "lab"
    if name.startswith(("guest_", "host_", "file_")):
        return "worker"
    if name.startswith(("vm_", "checkpoint", "media_", "console_")):
        return "wmi"
    return "service"


@contextmanager
def correlated(correlation_id: str | None = None) -> Iterator[str]:
    """Group every line published inside this scope under one id."""
    token = _correlation.set(correlation_id or uuid.uuid4().hex)
    try:
        yield _correlation.get() or ""
    finally:
        _correlation.reset(token)


def current_correlation() -> str | None:
    return _correlation.get()


@dataclass(frozen=True, slots=True)
class LogRecord:
    sequence: int
    timestamp: str
    source: str
    vm: str | None
    level: str
    correlation_id: str | None
    message: str


class LogBus:
    """A bounded in-memory stream with an offset, plus a daily JSONL file.

    Reads take no lock: a reader slices the list while a writer appends to it,
    which CPython makes safe, and that is the property the dock needs — a
    subscription held open for half a minute must not stall the service.
    """

    def __init__(self, directory: Path | None = None, *, max_records: int = _MAX_RECORDS) -> None:
        self._records: list[LogRecord] = []
        self._dropped = 0
        self._sequence = 0
        self._max_records = max(1, max_records)
        self._directory = directory
        self._condition = threading.Condition()

    @property
    def latest_sequence(self) -> int:
        return self._sequence

    def publish(
        self,
        source: str,
        message: Any,
        *,
        vm: str | None = None,
        level: str = "info",
        correlation_id: str | None = None,
    ) -> LogRecord:
        name = _source(source)
        severity = _level(level)
        text = redact(_clip(message))
        with self._condition:
            self._sequence += 1
            record = LogRecord(
                sequence=self._sequence,
                timestamp=datetime.now(timezone.utc).isoformat(),
                source=name,
                vm=_clip(vm, 256) or None,
                level=severity,
                correlation_id=correlation_id or _correlation.get(),
                message=text,
            )
            self._records.append(record)
            overflow = len(self._records) - self._max_records
            if overflow > 0:
                del self._records[:overflow]
                self._dropped += overflow
            self._condition.notify_all()
        self._persist(record)
        return record

    def snapshot(
        self,
        after: int = 0,
        *,
        vm: str | None = None,
        source: str | None = None,
        level: str | None = None,
        correlation_id: str | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        wanted_vm = str(vm or "").casefold()
        wanted_source = str(source or "").casefold()
        wanted_level = _level(level) if level else None
        start = max(0, int(after) - self._dropped)
        records = []
        for record in self._records[start:]:
            if wanted_vm and (record.vm or "").casefold() != wanted_vm:
                continue
            if wanted_source and record.source != wanted_source:
                continue
            if wanted_level and _LEVEL_INDEX[record.level] < _LEVEL_INDEX[wanted_level]:
                continue
            if correlation_id and record.correlation_id != correlation_id:
                continue
            records.append(asdict(record))
        return {"records": records[-max(1, limit):], "latest_sequence": self._sequence}

    def wait(
        self,
        after: int = 0,
        *,
        timeout_seconds: float = 15.0,
        **filters: Any,
    ) -> dict[str, Any]:
        """Hold the caller until a matching line arrives, or the time runs out."""
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._condition:
            while not self._has_new(after, filters):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
        return self.snapshot(after, **filters)

    def _has_new(self, after: int, filters: dict[str, Any]) -> bool:
        answer = self.snapshot(after, limit=1, **filters)
        return bool(answer["records"])

    def _persist(self, record: LogRecord) -> None:
        try:
            from .config import Settings

            settings = Settings.from_env()
            directory = self._directory or settings.event_directory
            directory = Path(directory).expanduser()
            directory.mkdir(parents=True, exist_ok=True)
            day = record.timestamp[:10].replace("-", "")
            path = directory / f"logs-{day}.jsonl"
            with path.open("a", encoding="utf-8", newline="") as stream:
                stream.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
            _prune(directory, settings.event_retention_days)
        except (OSError, ValidationError):
            # A log that cannot be written must not fail the operation it describes.
            pass


_pruned: set[str] = set()
_prune_lock = threading.Lock()


def _prune(directory: Path, retention_days: int) -> None:
    key = str(directory)
    with _prune_lock:
        if key in _pruned:
            return
        _pruned.add(key)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y%m%d")
    for path in directory.glob("logs-*.jsonl"):
        if path.stem[len("logs-"):] >= cutoff:
            continue
        try:
            path.unlink()
        except OSError:
            pass


LOGS = LogBus()
