"""Drive ``kd.exe`` headless over stdio, with markers instead of sleeps.

The protocol: before every command the session prints a unique marker with
``.echo``, then the command, then a second marker. The text between the two is
the command's output, and the end marker proves that kd finished with it — no
timeout is ever used to decide that a command is done.

Ideas reused from ``mcp-windbg`` 1.6.0 (MIT, Copyright (c) 2025 Sven
Scharmentke): the per-session random nonce inside the marker, a separate path
for commands that resume the target (``g`` and friends) which must never wait
for a marker, and CTRL_BREAK_EVENT on a child created in its own process group
as the break-in. The code here is independent and narrower: one session per VM,
no web server, no user-mode support.
"""

from __future__ import annotations

import codecs
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from ..errors import DebuggerError

MARKER_PREFIX = "HVMCPKD"
MARKER_END_SUFFIX = "END"
# kd's kernel prompt: "kd> " and "0: kd> " (also "1: kd>" for a second CPU).
# The prompt ends the buffer, so text after it means the target is not stopped.
PROMPT_RE = re.compile(r"(?:^|\n)(?:\d+:)?\s*kd>[ \t]*$")
PROMPT_PREFIX_RE = re.compile(r"^(?:\d+:)?\s*kd>[ \t]?", re.MULTILINE)
CONNECTED_BANNERS = ("Connected to target", "Kernel Debugger connection established")
GO_COMMANDS = frozenset({"g", "gh", "gn", "gc", "gu"})
BUGCHECK_MARKERS = ("*** Fatal System Error", "*** Bugcheck", "*** STOP:")
MODULE_LOAD_RE = re.compile(r"^ModLoad:\s+([0-9a-fA-F`]+)\s+([0-9a-fA-F`]+)\s+(\S+)", re.MULTILINE)
MAX_COMMAND_CHARS = 4_096
# Kept in memory for output extraction; the log file keeps everything.
_BUFFER_CHARS = 4 * 1024 * 1024
_READ_CHUNK = 4_096
_TRUNCATED_HEAD = 0.6
# How often a tailing reader looks again while it waits for kd to write.
_TAIL_POLL = 0.05


def _read_from(path: Path, offset: int) -> tuple[str, int]:
    """What the log gained since ``offset``, as text and as a new offset.

    The trailing bytes of an incomplete character are held back rather than
    replaced: a multi-byte character split across two reads would otherwise
    arrive as a replacement mark and be lost for good.
    """
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if size <= offset:
                return "", size
            stream.seek(offset)
            data = stream.read(size - offset)
    except OSError:
        return "", offset
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    text = decoder.decode(data, final=False)
    pending, _ = decoder.getstate()
    return text, offset + len(data) - len(pending)


def is_go_command(command: str) -> bool:
    """True for run-control commands that resume the target and return at a break."""
    head = re.split(r"[\s;]", command.strip(), maxsplit=1)[0] if command.strip() else ""
    return head.casefold() in GO_COMMANDS


def _ends_with_prompt(text: str) -> bool:
    return PROMPT_RE.search(text[-512:]) is not None


def _truncate(text: str, limit: int) -> tuple[str, int]:
    """Keep the head and the tail, marking what was dropped."""
    if len(text) <= limit:
        return text, 0
    head = int(limit * _TRUNCATED_HEAD)
    tail = limit - head
    omitted = len(text) - limit
    marker = f"\n[... {omitted} characters omitted by hyperv-console-mcp ...]\n"
    return text[:head] + marker + (text[-tail:] if tail else ""), omitted


def _clean_output(
    text: str, marker: str | None = None, command: str | None = None
) -> str:
    """Drop marker echoes, prompt prefixes, and the debugger's echo of the command."""
    lines: list[str] = []
    skipped_echo = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if marker is not None and marker in line:
            continue
        stripped = PROMPT_PREFIX_RE.sub("", line)
        if not skipped_echo and command is not None and stripped.strip() == command:
            skipped_echo = True
            continue
        lines.append(stripped)
    return "\n".join(lines).strip("\n")


class KdSession:
    """One kd.exe process, one target, one state machine.

    States: ``disconnected -> waiting_for_target -> broken <-> running``, plus
    ``disconnected`` again after detach or an exit of kd.exe.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        log_path: Path | str,
        vm_name: str = "",
        transport: str = "kdnet",
        symbol_path: str | None = None,
        max_output_chars: int = 65_536,
        timeout: float = 30.0,
        process_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._command = [str(part) for part in command]
        self.vm_name = vm_name
        self.transport = transport
        self.symbol_path = symbol_path
        self.log_path = Path(log_path)
        self._max_output = max(1_024, int(max_output_chars))
        self._default_timeout = max(0.1, float(timeout))
        self._factory = process_factory or subprocess.Popen
        self._process: Any = None
        self._reader: threading.Thread | None = None
        self._log: Any = None
        self._log_lock = threading.Lock()
        self._condition = threading.Condition()
        self._text = ""
        self._dropped = 0
        self._total = 0
        self._eof = False
        self._reader_error: str | None = None
        self._state = "disconnected"
        self._nonce = secrets.token_hex(8)
        self._sequence = 0
        self._commands = 0
        self._started_at: str | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def output_offset(self) -> int:
        """How many bytes of this session's log exist right now.

        A subscriber that starts here sees what happens next rather than the
        whole session so far, which is what a dock opened mid-run wants.
        """
        try:
            return self.log_path.stat().st_size
        except OSError:
            return 0

    def _set_state(self, state: str) -> None:
        with self._condition:
            self._state = state
            self._condition.notify_all()

    def _environment(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.symbol_path:
            env["_NT_SYMBOL_PATH"] = self.symbol_path
        return env

    def start(self, *, startup_timeout: float = 60.0) -> dict[str, Any]:
        if self._process is not None:
            raise DebuggerError("The debugger session is already running.", code="kd_already_connected")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self.log_path, "a", encoding="utf-8", newline="")
        flags = 0
        if os.name == "nt":
            # Own process group so CTRL_BREAK_EVENT reaches kd and nothing else.
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            self._process = self._factory(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self._environment(),
                creationflags=flags,
                bufsize=0,
            )
        except OSError as exc:
            self._close_log()
            raise DebuggerError(
                f"Could not start {Path(self._command[0]).name}: {exc}", code="kd_start_failed"
            ) from exc
        self._started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._set_state("waiting_for_target")
        self._reader = threading.Thread(
            target=self._read_loop, daemon=True, name=f"hvmcp-kd-{self.vm_name or 'session'}"
        )
        self._reader.start()
        start = self._position()
        if not self._wait(
            lambda: _ends_with_prompt(self._slice(start))
            or any(banner in self._slice(start) for banner in CONNECTED_BANNERS),
            startup_timeout,
        ):
            self._set_state("waiting_for_target")
        else:
            self._set_state("broken")
        return self.status()

    def close(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except Exception:
                pass
        if self._reader is not None:
            self._reader.join(timeout=3)
            self._reader = None
        if process is not None:
            for stream in (process.stdin, process.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
            try:
                process.wait(timeout=5)
            except Exception:
                pass
        self._process = None
        self._close_log()
        self._set_state("disconnected")

    def _close_log(self) -> None:
        with self._log_lock:
            if self._log is not None:
                try:
                    self._log.close()
                except Exception:
                    pass
                self._log = None

    def detach(self, *, timeout: float = 10.0) -> dict[str, Any]:
        """Ask kd to detach (``qd``) and wait for it to exit."""
        if self._process is None:
            self._set_state("disconnected")
            return self.status()
        if self._state == "broken" and not self._eof:
            try:
                self._write("qd\n")
            except DebuggerError:
                pass
            self._wait(lambda: self._eof, timeout)
        self.close()
        return self.status()

    # -- reader and buffer -------------------------------------------------

    def _read_loop(self) -> None:
        stream = self._process.stdout
        read = getattr(stream, "read1", None) or stream.read
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                chunk = read(_READ_CHUNK)
                if not chunk:
                    break
                self._append(decoder.decode(chunk))
            tail = decoder.decode(b"", final=True)
            if tail:
                self._append(tail)
        except Exception as exc:
            self._reader_error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._condition:
                self._eof = True
                self._condition.notify_all()
            if self._state != "disconnected":
                self._set_state("disconnected")

    def _append(self, text: str) -> None:
        if not text:
            return
        with self._log_lock:
            if self._log is not None:
                try:
                    self._log.write(text)
                    self._log.flush()
                except Exception:
                    pass
        with self._condition:
            self._text += text
            self._total += len(text)
            if len(self._text) > _BUFFER_CHARS:
                drop = len(self._text) - _BUFFER_CHARS
                self._text = self._text[drop:]
                self._dropped += drop
            self._condition.notify_all()

    def _position(self) -> int:
        with self._condition:
            return self._total

    def _slice(self, start: int) -> str:
        with self._condition:
            return self._text[max(0, start - self._dropped):]

    def _wait(self, predicate: Callable[[], bool], timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                if predicate():
                    return True
                if self._eof:
                    return predicate()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(remaining, 0.25))

    # -- writing -----------------------------------------------------------

    def _write(self, text: str) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise DebuggerError("No debugger session is running.", code="kd_not_connected")
        if process.poll() is not None:
            raise DebuggerError("kd.exe exited.", code="kd_exited")
        try:
            process.stdin.write(text.encode("utf-8"))
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise DebuggerError(f"Writing to kd.exe failed: {exc}", code="kd_exited") from exc

    def _next_marker(self) -> str:
        with self._condition:
            self._sequence += 1
            return f"{MARKER_PREFIX}{self._nonce}{self._sequence:04d}"

    # -- commands ----------------------------------------------------------

    def command(self, command: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Run one command and return its output, delimited by markers."""
        text = self._validate(command)
        if is_go_command(text):
            return self.go(text, timeout=timeout)
        if self._state in {"disconnected"} or self._process is None:
            raise DebuggerError("No debugger session is running.", code="kd_not_connected")
        if self._state == "running":
            raise DebuggerError(
                "The target is running; break in before sending commands.", code="kd_running"
            )
        limit = self._default_timeout if timeout is None else max(0.1, float(timeout))
        marker = self._next_marker()
        end_marker = marker + MARKER_END_SUFFIX
        start = self._position()
        started = time.monotonic()
        self._write(f".echo {marker}\n")
        self._write(text + "\n")
        self._write(f".echo {end_marker}\n")
        self._commands += 1
        if not self._wait(lambda: end_marker in self._slice(start), limit):
            # The command was abandoned. If it left the target running, ask for
            # control back; the unique sequence in every marker makes a late
            # reply from this command harmless to the commands that follow.
            if self._state == "running":
                try:
                    self._signal_break()
                    self._wait(lambda: _ends_with_prompt(self._slice(start)), 2.0)
                except DebuggerError:
                    pass
                if self._state != "disconnected":
                    self._set_state("broken")
            raise DebuggerError(
                f"Command timed out after {limit:.0f}s: {text[:120]}",
                code="kd_command_timeout",
            )
        segment = self._slice(start)
        end_index = segment.rfind(end_marker)
        body = segment[:end_index]
        begin_index = body.find(marker)
        if begin_index >= 0:
            body = body[begin_index + len(marker):]
        output = _clean_output(body, marker, text)
        truncated, omitted = _truncate(output, self._max_output)
        self._set_state("broken")
        return {
            "command": text,
            "output": truncated,
            "truncated": bool(omitted),
            "omitted_chars": omitted,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "state": self._state,
        }

    def go(self, command: str = "g", *, timeout: float | None = None) -> dict[str, Any]:
        """Resume the target and wait for it to break again (or time out)."""
        text = self._validate(command)
        if self._state != "broken":
            raise DebuggerError(
                f"The target must be broken to resume it (state: {self._state}).",
                code="kd_not_broken",
            )
        limit = self._default_timeout if timeout is None else max(0.1, float(timeout))
        start = self._position()
        started = time.monotonic()
        self._write(text + "\n")
        self._commands += 1
        self._set_state("running")
        broke = self._wait(lambda: _ends_with_prompt(self._slice(start)), limit)
        if broke:
            self._set_state("broken")
        output = _clean_output(self._slice(start), command=text)
        truncated, omitted = _truncate(output, self._max_output)
        return {
            "command": text,
            "output": truncated,
            "truncated": bool(omitted),
            "omitted_chars": omitted,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "timed_out": not broke,
            "broke": broke,
            "state": self._state,
        }

    def break_in(self, *, timeout: float = 15.0) -> dict[str, Any]:
        """Interrupt a running target and wait for the prompt."""
        if self._state == "disconnected" or self._process is None:
            raise DebuggerError("No debugger session is running.", code="kd_not_connected")
        if self._state == "broken":
            return {"requested": True, "already_broken": True, "broke": True, "state": self._state}
        start = self._position()
        self._signal_break()
        broke = self._wait(lambda: _ends_with_prompt(self._slice(start)), timeout)
        if broke:
            self._set_state("broken")
        return {
            "requested": True,
            "already_broken": False,
            "broke": broke,
            "state": self._state,
            "output": _truncate(_clean_output(self._slice(start)), self._max_output)[0],
        }

    def _signal_break(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.send_signal(signal.SIGINT)
        except (OSError, ValueError, AttributeError) as exc:
            raise DebuggerError(
                f"Sending the break-in failed: {exc}. Use vm_inject_nmi as a fallback.",
                code="kd_break_failed",
            ) from exc

    def wait_event(self, *, timeout: float = 60.0) -> dict[str, Any]:
        """Block until the target breaks, a bugcheck or a module load shows up."""
        if self._state == "disconnected" or self._process is None:
            raise DebuggerError("No debugger session is running.", code="kd_not_connected")
        start = self._position()
        started = time.monotonic()

        def classify() -> str | None:
            text = self._slice(start)
            if any(marker in text for marker in BUGCHECK_MARKERS):
                return "bugcheck"
            if MODULE_LOAD_RE.search(text):
                return "module_load"
            if _ends_with_prompt(text):
                return "break"
            if self._eof:
                return "exited"
            return None

        event = "timeout"
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._condition:
                found = classify()
                if found is not None:
                    event = found
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(min(remaining, 0.25))
        if event in {"break", "bugcheck"}:
            self._set_state("broken")
        text = self._slice(start)
        module = None
        match = MODULE_LOAD_RE.search(text)
        if match:
            module = match.group(3)
        return {
            "event": event,
            "module": module,
            "state": self._state,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "output": _truncate(_clean_output(text), self._max_output)[0],
        }

    # -- introspection -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._condition:
            text_chars = self._total
            state = self._state
        process = self._process
        return {
            "state": state,
            "alive": bool(process is not None and process.poll() is None),
            "pid": int(process.pid) if process is not None and process.pid else None,
            "vm": self.vm_name or None,
            "transport": self.transport,
            "log_path": str(self.log_path),
            "started_at": self._started_at,
            "commands": self._commands,
            "output_chars": text_chars,
            "reader_error": self._reader_error,
            "symbol_path_set": bool(self.symbol_path),
        }

    def read_output(self, after: int = 0, timeout: float = 0.0) -> dict[str, Any]:
        """Follow the session log from a byte offset, holding until it grows.

        The log is what kd wrote, flushed as it arrived, so this follows output
        nobody asked for — a module load, a break-in, a bugcheck — and not only
        the text a command returned. The offset is the whole protocol: a reader
        that keeps it gets every line once, in order, whoever typed.
        """
        offset = max(0, int(after))
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            text, offset = _read_from(self.log_path, offset)
            if text or time.monotonic() >= deadline:
                break
            time.sleep(_TAIL_POLL)
        return {
            "text": text,
            "offset": offset,
            "state": self._state,
            "alive": self.alive,
            "log_path": str(self.log_path),
        }

    @staticmethod
    def _validate(command: str) -> str:
        if not isinstance(command, str) or not command.strip():
            raise DebuggerError("A debugger command must be a non-empty string.", code="kd_invalid_command")
        if "\n" in command or "\r" in command:
            raise DebuggerError(
                "A debugger command must be a single line; separate commands with ';'.",
                code="kd_invalid_command",
            )
        if len(command) > MAX_COMMAND_CHARS:
            raise DebuggerError(
                f"A debugger command is limited to {MAX_COMMAND_CHARS} characters.",
                code="kd_invalid_command",
            )
        return command.strip()


def run_dump(
    kd_path: Path | str,
    dump_path: Path | str,
    *,
    commands: Sequence[str] = ("!analyze -v",),
    timeout: float = 120.0,
    symbol_path: str | None = None,
    max_output_chars: int = 262_144,
    process_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run kd non-interactively against a crash dump (``kd -z``)."""
    dump = Path(dump_path)
    if not dump.is_file():
        raise DebuggerError(f"Dump file not found: {dump}", code="kd_dump_not_found")
    script = "; ".join([*commands, "q"])
    env = dict(os.environ)
    if symbol_path:
        env["_NT_SYMBOL_PATH"] = symbol_path
    factory = process_factory or subprocess.Popen
    started = time.monotonic()
    try:
        process = factory(
            [str(kd_path), "-z", str(dump), "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
    except OSError as exc:
        raise DebuggerError(f"Could not start kd.exe: {exc}", code="kd_start_failed") from exc
    try:
        raw, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise DebuggerError(
            f"Analyzing {dump.name} timed out after {timeout:.0f}s.", code="kd_dump_timeout"
        ) from None
    text = (raw or b"").decode("utf-8", "replace")
    truncated, omitted = _truncate(text, max_output_chars)
    return {
        "dump": str(dump),
        "exit_code": process.returncode,
        "output": truncated,
        "truncated": bool(omitted),
        "omitted_chars": omitted,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


__all__ = [
    "BUGCHECK_MARKERS",
    "CONNECTED_BANNERS",
    "GO_COMMANDS",
    "KdSession",
    "MARKER_PREFIX",
    "PROMPT_RE",
    "is_go_command",
    "run_dump",
]
