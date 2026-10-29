"""A tiny kd.exe stand-in for the session tests.

It speaks the same protocol the real debugger does as far as KdSession cares:
a banner, ``0: kd> `` prompts, ``.echo`` markers, run-control commands that
return only when the target breaks, and a break-in delivered as CTRL_BREAK
(SIGBREAK) on Windows or SIGINT elsewhere.
"""

from __future__ import annotations

import queue
import signal
import sys
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_state = {"broken": True, "quit": False}
_lines: queue.Queue[str | None] = queue.Queue()
_scenario = "normal"
_analyze_file: Path | None = None


def out(text: str) -> None:
    with _lock:
        sys.stdout.write(text)
        sys.stdout.flush()


def prompt() -> None:
    out("0: kd> ")


def on_break(signum, frame) -> None:  # noqa: ARG001 - signal handler signature
    if not _state["broken"]:
        _state["broken"] = True
        out("\nBreak instruction exception - code 80000003 (first chance)\n")
        out("nt!RtlpBreakWithStatusInstruction\n")
        prompt()


def install_handlers() -> None:
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, on_break)
    signal.signal(signal.SIGINT, on_break)


def reader() -> None:
    for line in sys.stdin:
        _lines.put(line.rstrip("\r\n"))
    _lines.put(None)


def break_after_bugcheck() -> None:
    _state["broken"] = True
    out("\n*** Fatal System Error: 0x000000d1\n")
    out("       (0x0000000000000010,0x0000000000000002,0x0000000000000000,0xfffff80512345678)\n")
    out("Driver at fault: MyDriver.sys.\n")
    prompt()


def module_load() -> None:
    # A module load is an event while the target keeps running: no prompt.
    out("\nModLoad: fffff805`12340000 fffff805`12380000   MyDriver.sys\n")


def handle(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith(".echo "):
        out(stripped[6:] + "\n")
        prompt()
        return True
    if stripped in {"g", "gh", "gn", "gc", "gu"}:
        _state["broken"] = False
        out("Continuing...\n")
        if _scenario == "never_break":
            pass
        elif _scenario == "module":
            threading.Timer(0.3, module_load).start()
        else:
            threading.Timer(0.05, break_after_bugcheck).start()
        return True
    if stripped == "qd":
        out("quit:\n")
        _state["quit"] = True
        return True
    if stripped == ".exit":
        return False
    if stripped == ".hang":
        # A hung command blocks kd's input parser: nothing that follows is read.
        out("hanging...\n")
        time.sleep(1.0)
        prompt()
        return True
    if stripped == "lm":
        out("start             end                 module name\n")
        out("fffff805`10000000 fffff805`10080000   MyDriver   (deferred)\n")
        prompt()
        return True
    if stripped == "k":
        out(" # Child-SP          RetAddr               Call Site\n")
        out("00 fffff805`0a1b2c30 fffff805`12345678     MyDriver!Crash+0x18\n")
        prompt()
        return True
    if stripped == "big":
        out("A" * 5000 + "\n")
        prompt()
        return True
    if stripped == "!analyze -v":
        if _analyze_file is not None:
            out(_analyze_file.read_text(encoding="utf-8"))
        else:
            out("No bugcheck analysis available.\n")
        prompt()
        return True
    if stripped.split(" ")[0] in {"bp", "bu", "ba", "sxe", "sxd"}:
        out("breakpoint set\n")
        prompt()
        return True
    out(f"No export {stripped.split(' ')[0]} found\n")
    prompt()
    return True


def main() -> int:
    global _scenario, _analyze_file
    arguments = sys.argv[1:]
    while arguments:
        argument = arguments.pop(0)
        if argument == "--scenario" and arguments:
            _scenario = arguments.pop(0)
        elif argument == "--analyze" and arguments:
            _analyze_file = Path(arguments.pop(0))
    install_handlers()
    threading.Thread(target=reader, daemon=True).start()
    out("Microsoft (R) Windows Debugger Version 10.0.26100.2454 AMD64\n")
    out("Connected to target 169.254.1.2 on port 50000 on local IP 10.77.0.1.\n")
    out("Kernel Debugger connection established.  (Initial Breakpoint requested)\n")
    prompt()
    while True:
        try:
            line = _lines.get(timeout=0.02)
        except queue.Empty:
            if _state["quit"]:
                return 0
            continue
        if line is None:
            return 0
        out(line + "\n")  # kd echoes what it reads
        if not handle(line):
            return 0


if __name__ == "__main__":
    sys.exit(main())
