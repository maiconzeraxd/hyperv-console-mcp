"""Whether kd resolved a module's symbols, and from which PDB.

``lm`` prints one line per module with the symbol state in parentheses, and
``!lmi`` prints what the debugger made of the image's debug directory. Both are
parsed here, because the lab flow has to say whether the stack it reports means
anything: a staged driver whose PDB did not load produces raw addresses and a
"Probably caused by" that names a module but no function.
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import ValidationError

# A kd argument, not a path: `!lmi C:\x\y.sys` is a syntax error, and a name
# with a separator would reach the debugger as a second token.
_NAME = re.compile(r"[A-Za-z0-9_.-]+")

_LM_ROW = re.compile(
    r"^(?P<start>[0-9a-f`]{8,17})\s+(?P<end>[0-9a-f`]{8,17})\s+(?P<module>\S+)(?P<rest>.*)$",
    re.IGNORECASE,
)
_LM_STATUS = re.compile(r"\((?P<status>[^()]*)\)")
_PDB_PATH = re.compile(r"(?:[A-Za-z]:\\|\\\\)[^()\s]+\.pdb", re.IGNORECASE)

_LMI_HEADER = re.compile(
    r"^\s*Loaded Module Info:\s*\[(?P<module>[^\]]+)\]", re.IGNORECASE | re.MULTILINE
)
_LMI_FIELD = re.compile(
    r"^\s*(?P<key>[A-Za-z][A-Za-z ]{1,24}):\s*(?P<value>.*)$", re.MULTILINE
)
_LMI_PDB = re.compile(r"Pdb:\s*(?P<pdb>[^\r\n]+?)\s*$", re.IGNORECASE | re.MULTILINE)
_LMI_BASE = re.compile(
    r"^\s*Base Address:\s*(?P<base>[0-9a-fA-F`]+)\s*$", re.IGNORECASE | re.MULTILINE
)

# `lm` status text -> what it means. Longest first: "(private pdb symbols)"
# contains "(pdb symbols)", and "(export symbols, deferred)" is still export.
_LM_KINDS = (
    ("private pdb symbols", "private_pdb"),
    ("pdb symbols", "pdb"),
    ("export symbols", "export"),
    ("no symbols", "none"),
    ("deferred", "deferred"),
)
LOADED_KINDS = frozenset({"private_pdb", "pdb"})
MAX_PDB_CHARS = 1_024


def module_name(value: str) -> str:
    """The bare module name kd accepts, with any ``.sys`` suffix removed."""
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValidationError("module must be a module name without a path or extension tricks.")
    return value[:-4] if value.casefold().endswith(".sys") else value


def _address(value: str) -> str:
    return "0x" + value.strip().replace("`", "").lower()


def _pdb_path(text: str) -> str | None:
    match = _PDB_PATH.search(text)
    if match is None or len(match.group(0)) > MAX_PDB_CHARS:
        return None
    return match.group(0)


def _status_kind(status: str) -> str:
    text = status.casefold()
    for needle, kind in _LM_KINDS:
        if needle in text:
            return kind
    return "unknown"


def parse_lm(text: str, module: str) -> dict[str, Any]:
    """The ``lm`` line for one module: its range, its symbol state, its PDB."""
    wanted = module.casefold()
    for line in text.splitlines():
        match = _LM_ROW.match(line.strip())
        if match is None or match.group("module").casefold() != wanted:
            continue
        rest = match.group("rest").strip()
        status = _LM_STATUS.search(rest)
        kind = _status_kind(status.group("status")) if status else "unknown"
        entry: dict[str, Any] = {
            "module": module,
            "found": True,
            "loaded": kind in LOADED_KINDS,
            "symbol_kind": kind,
            "start": _address(match.group("start")),
            "end": _address(match.group("end")),
            "source": "lm",
        }
        if status is not None:
            entry["status"] = status.group("status").strip()
        pdb = _pdb_path(rest)
        if pdb:
            entry["pdb"] = pdb
        return entry
    return {"module": module, "found": False, "loaded": None, "source": "lm"}


def _lmi_kind(symbol_type: str, report: str) -> str:
    kind = symbol_type.casefold() or "unknown"
    if kind != "pdb":
        return kind
    # A PDB that was found but rejected still reads as a failure: kd wraps the
    # reason in asterisks, and the stack will not have names.
    if "***" in report:
        return "rejected"
    return "private_pdb" if "private symbols" in report.casefold() else "pdb"


def parse_lmi(text: str, module: str) -> dict[str, Any]:
    """What ``!lmi`` says the debugger did with the image's symbols.

    ``Symbol Type`` is the verdict: ``PDB`` means a file was loaded, ``EXPORT``
    means none was found, and ``MISMATCH`` means the one found belongs to a
    different build. ``Load Report`` carries the detail that decides whether the
    file was usable.
    """
    header = _LMI_HEADER.search(text)
    if header is None or header.group("module").casefold() != module.casefold():
        return {"module": module, "found": False, "loaded": None, "source": "!lmi"}
    fields = {
        match.group("key").strip().casefold(): match.group("value").strip()
        for match in _LMI_FIELD.finditer(text)
    }
    symbol_type, _, detail = fields.get("symbol type", "").partition("-")
    report = fields.get("load report", "")
    kind = _lmi_kind(symbol_type.strip(), report)
    entry: dict[str, Any] = {
        "module": module,
        "found": True,
        "loaded": kind in LOADED_KINDS,
        "symbol_kind": kind,
        "source": "!lmi",
    }
    if symbol_type.strip():
        entry["symbol_type"] = symbol_type.strip()
    if detail.strip():
        entry["symbol_detail"] = detail.strip()[:MAX_PDB_CHARS]
    if report:
        entry["load_report"] = report[:MAX_PDB_CHARS]
    pdb = _LMI_PDB.search(text)
    path = pdb.group("pdb").strip() if pdb is not None else _pdb_path(text)
    if path and len(path) <= MAX_PDB_CHARS:
        entry["pdb"] = path
    base = _LMI_BASE.search(text)
    if base is not None:
        entry["base"] = _address(base.group("base"))
    return entry


def read(text: str, module: str) -> dict[str, Any]:
    """The best answer either command gave for one module."""
    result = parse_lmi(text, module)
    return result if result["found"] else parse_lm(text, module)


__all__ = ["LOADED_KINDS", "module_name", "parse_lm", "parse_lmi", "read"]
