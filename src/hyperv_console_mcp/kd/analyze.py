"""Parse ``!analyze -v`` output into the fields an agent acts on.

The parser is deliberately tolerant: WinDbg prints a different set of fields for
every bugcheck class, and older/newer debuggers word the same field
differently. Anything missing is simply absent from the result rather than
guessed at.
"""

from __future__ import annotations

import re
from typing import Any

# "BUGCHECK_CODE:  d1" / "BUGCHECK_P1: ffffffff`ffffffff"
_FIELD = re.compile(r"^([A-Z][A-Z0-9_]{2,40}):\s*(\S.*?)\s*$")
# "Probably caused by : MyDriver.sys ( MyDriver!Crash+0x1a )"
_PROBABLY = re.compile(r"^Probably caused by\s*:\s*(.+?)\s*$", re.IGNORECASE)
# "fffff800`12345678 fffff800`87654321 : 00000000`0000000a ... : nt!KeBugCheckEx"
_FRAME = re.compile(
    r"^([0-9a-fA-F`]{6,17})\s+([0-9a-fA-F`]{6,17})\s*:\s*(.+?)\s*$"
)
_BACKTICK = re.compile(r"`")
_BUGCHECK_PREFIX = "BUGCHECK_P"

# Fields copied verbatim (lower-cased) when present.
_TEXT_FIELDS = {
    "BUGCHECK_STR": "bugcheck_str",
    "DEFAULT_BUCKET_ID": "default_bucket_id",
    "FAILURE_BUCKET_ID": "failure_bucket_id",
    "PROCESS_NAME": "process_name",
    "IMAGE_NAME": "image_name",
    "MODULE_NAME": "module_name",
    "FAULTING_MODULE": "faulting_module",
    "SYMBOL_NAME": "symbol_name",
    "STACK_COMMAND": "stack_command",
    "OSNAME": "os_name",
    "OS_VERSION": "os_version",
    "EXCEPTION_CODE": "exception_code",
    "EXCEPTION_CODE_STR": "exception_code_str",
    "FAILURE_ID_HASH": "failure_id_hash",
    "ANALYSIS_SESSION_HOST": "analysis_host",
}
MAX_TOP_FRAMES = 10


def _hex(value: str) -> str:
    """Normalise a debugger address or code to 0x-prefixed lowercase hex."""
    cleaned = _BACKTICK.sub("", value.strip()).lower()
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    return f"0x{cleaned}" if re.fullmatch(r"[0-9a-f]+", cleaned) else value.strip()


def _stack_frames(lines: list[str], start: int) -> list[dict[str, Any]]:
    """Collect frames from the STACK_TEXT block that begins at ``start``."""
    frames: list[dict[str, Any]] = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            if frames:
                break
            continue
        if _FIELD.match(stripped) or _PROBABLY.match(stripped):
            break
        match = _FRAME.match(stripped)
        if not match:
            continue
        address, return_address, rest = match.groups()
        # The trailing " : symbol" is optional; the arguments in the middle are
        # noise for an agent trying to find the faulting frame.
        symbol = rest.rsplit(" : ", 1)[-1].strip()
        frames.append(
            {
                "frame": len(frames),
                "address": _hex(address),
                "return_address": _hex(return_address),
                "symbol": symbol,
            }
        )
    return frames


def analyze_report(text: str) -> dict[str, Any]:
    """Structure the interesting parts of a ``!analyze -v`` dump."""
    lines = text.splitlines()
    fields: dict[str, str] = {}
    probably: str | None = None
    stack_start: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stack_start is None and stripped.upper().startswith("STACK_TEXT"):
            stack_start = index + 1
            continue
        match = _PROBABLY.match(stripped)
        if match and probably is None:
            probably = match.group(1)
            continue
        match = _FIELD.match(stripped)
        if match and match.group(1) not in fields:
            fields[match.group(1)] = match.group(2)

    report: dict[str, Any] = {"found": False}
    code = fields.get("BUGCHECK_CODE") or fields.get("BUGCHECK_CODE_STR")
    parameters = [
        _hex(fields[key]) for key in sorted(fields) if key.startswith(_BUGCHECK_PREFIX)
    ]
    if code:
        report["bugcheck_code"] = _hex(code)
        try:
            report["bugcheck_code_int"] = int(_hex(code), 16)
        except ValueError:
            pass
        report["found"] = True
    if parameters:
        report["bugcheck_parameters"] = parameters
    for key, name in _TEXT_FIELDS.items():
        if key in fields:
            report[name] = fields[key]
    if probably:
        report["probably_caused_by"] = probably
        # "MyDriver.sys ( MyDriver!Crash+0x1a )" -> driver and symbol.
        driver = probably.split("(", 1)[0].strip()
        if driver:
            report["blamed_driver"] = driver
        inner = probably.split("(", 1)[1].rstrip(" )").strip() if "(" in probably else ""
        if inner and "!name" not in inner:
            report.setdefault("symbol_name", inner)
    if stack_start is not None:
        frames = _stack_frames(lines, stack_start)
        if frames:
            report["stack_frames"] = frames
            report["top_frames"] = frames[:MAX_TOP_FRAMES]
            report["found"] = True
    if "symbol_name" in report and "module_name" not in report and "!" in report["symbol_name"]:
        report["module_name"] = report["symbol_name"].split("!", 1)[0]
    return report


__all__ = ["MAX_TOP_FRAMES", "analyze_report"]
