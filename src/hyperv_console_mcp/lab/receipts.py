"""The receipts a cycle leaves behind: list them, open one, compare two.

``lab.report.write`` puts one record per run beside its evidence, and that file
is the whole point of a receipt: it survives the transcript. This reads those
files back so a window can show what ran, what it cost and what it left, and so
"the same test, three runs apart" is a comparison of two records rather than a
memory of two answers.

Nothing here touches a VM or a guest. Every function is a pure read of the
evidence root, and a directory that holds no readable record is reported as
skipped rather than guessed at.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..errors import ValidationError
from .report import REPORT_NAME, SCHEMA

# A run id is a directory name under the evidence root. It is checked before it
# is joined, so a receipt can never be read from outside that root.
_RUN_ID = re.compile(r"[A-Za-z0-9_.-]+")
MAX_RECEIPTS = 200
MAX_DETAIL_CHARS = 300

# The fields a comparison looks at, in the order a reader wants them. A driver
# test that behaves differently between two runs differs in one of these; the
# timestamps and the hashes of the evidence are compared too, because "the
# evidence changed" is itself a finding.
COMPARED = (
    "package_id",
    "manifest_sha256",
    "verified",
    "files_verified",
    "outcome",
    "detail",
    "duration_seconds",
    "payload_executed",
    "debugger_transport",
    "debugger_event",
    "symbols_all_loaded",
    "bugcheck_code",
    "failure_bucket_id",
    "blamed_driver",
    "symbols_resolved",
    "evidence_files",
    "evidence_sha256",
    "checkpoint",
    "reverted",
)


def _run_id(value: Any) -> str:
    text = str(value or "").strip()
    # "." and ".." are directory names a regex of allowed characters happily
    # accepts; they are exactly the two that would leave the root.
    if not _RUN_ID.fullmatch(text) or not text.strip("."):
        raise ValidationError("run_id must be a plain directory name under the evidence root.")
    return text


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def view(record: dict[str, Any]) -> dict[str, Any]:
    """One record reduced to the fields two runs can be compared on."""
    package = _mapping(record.get("package"))
    crash = _mapping(record.get("crash"))
    debug = _mapping(record.get("debugger"))
    evidence = _mapping(record.get("evidence"))
    checkpoint = _mapping(record.get("checkpoint"))
    revert = checkpoint.get("revert")
    collected = bool(evidence.get("collected"))
    contents = [item for item in evidence.get("contents") or [] if isinstance(item, dict)]
    return {
        "vm": record.get("vm"),
        "run_id": record.get("run_id"),
        "started_utc": record.get("started_utc"),
        "finished_utc": record.get("finished_utc"),
        "package_id": package.get("package_id"),
        "manifest_sha256": package.get("manifest_sha256"),
        "verified": package.get("verified"),
        "files_verified": package.get("files_verified"),
        "outcome": record.get("outcome"),
        "detail": record.get("detail"),
        "duration_seconds": record.get("duration_seconds"),
        "payload_executed": record.get("payload_executed"),
        "debugger_transport": debug.get("transport"),
        "debugger_event": debug.get("event"),
        "symbols_all_loaded": debug.get("all_loaded"),
        "bugcheck_code": crash.get("bugcheck_code"),
        "failure_bucket_id": crash.get("failure_bucket_id"),
        "blamed_driver": crash.get("blamed_driver"),
        "symbols_resolved": crash.get("symbols_resolved"),
        "evidence_files": len(contents) if collected else 0,
        "evidence_sha256": evidence.get("sha256"),
        "checkpoint": checkpoint.get("name"),
        "reverted": _mapping(revert).get("restored") if isinstance(revert, dict) else revert,
    }


def crash_view(record: dict[str, Any]) -> dict[str, Any] | None:
    """What this receipt says about the crash, in the shape a triage panel reads.

    The record already reduced the crash to the fields worth keeping, so this
    is a rename rather than a reading: a run that bugchecked with no dump still
    answers with its code and parameters, and one that never crashed answers
    with nothing rather than with empty fields.
    """
    crash = _mapping(record.get("crash"))
    if not crash:
        return None
    return {
        "analyzed": crash.get("analyzed"),
        "dump": crash.get("dump"),
        "kd_log": crash.get("kd_log"),
        "bugcheck_code": crash.get("bugcheck_code"),
        "bugcheck_str": crash.get("bugcheck_str"),
        "bugcheck_parameters": crash.get("bugcheck_parameters") or [],
        "failure_bucket_id": crash.get("failure_bucket_id"),
        "default_bucket_id": crash.get("default_bucket_id"),
        "blamed_driver": crash.get("blamed_driver"),
        "symbol_name": crash.get("symbol_name"),
        "symbols_resolved": crash.get("symbols_resolved"),
        "top_frames": crash.get("top_frames") or [],
        "sources": crash.get("sources") or [],
    }


def read_record(root: Path, run_id: str) -> dict[str, Any] | None:
    """One receipt, or ``None`` when the run has no readable record.

    A record from a newer schema is refused rather than half-read: the fields
    this module compares are the ones the schema promised.
    """
    directory = Path(root) / _run_id(run_id)
    path = directory / REPORT_NAME
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        return None
    return {**record, "path": str(path), "sha256": hashlib.sha256(data).hexdigest()}


def evidence_files(root: Path, run_id: str, record: dict[str, Any]) -> list[dict[str, Any]]:
    """The files this run collected, with where they are now.

    The record names them by their path inside the run's directory; the path
    beside each entry is the absolute one, so a window can hand the operator a
    place to look instead of a name to search for.
    """
    evidence = _mapping(record.get("evidence"))
    if not evidence.get("collected"):
        return []
    directory = Path(root) / _run_id(run_id)
    found: list[dict[str, Any]] = []
    for item in evidence.get("contents") or []:
        if not isinstance(item, dict):
            continue
        relative = str(item.get("path") or "").replace("\\", "/").strip("/")
        if not relative or ".." in relative.split("/"):
            continue
        found.append({
            "path": relative,
            "host_path": str(directory / relative),
            "size": item.get("size"),
            "sha256": item.get("sha256"),
        })
    return found


def list_receipts(
    root: Path, *, vm_name: str | None = None, limit: int = 20
) -> dict[str, Any]:
    """Every run under the evidence root, newest first, for one VM or for all."""
    base = Path(root)
    try:
        children = sorted(base.iterdir()) if base.is_dir() else []
    except OSError as exc:
        return {"root": str(base), "count": 0, "receipts": [], "error": str(exc)[:MAX_DETAIL_CHARS]}
    receipts: list[dict[str, Any]] = []
    skipped: list[str] = []
    for child in children:
        try:
            if not child.is_dir() or _RUN_ID.fullmatch(child.name) is None:
                continue
        except OSError:
            continue
        record = read_record(base, child.name)
        if record is None:
            skipped.append(child.name)
            continue
        entry = view(record)
        if vm_name and str(entry.get("vm") or "").casefold() != vm_name.casefold():
            continue
        entry["path"] = record["path"]
        entry["sha256"] = record["sha256"]
        receipts.append(entry)
    # Newest first by the time the cycle finished. A record without one sorts
    # last rather than first: an unknown time is not a recent one.
    receipts.sort(key=lambda item: str(item.get("finished_utc") or ""), reverse=True)
    return {
        "root": str(base),
        "count": len(receipts),
        "receipts": receipts[:limit],
        "truncated": len(receipts) > limit,
        "skipped": skipped[:20],
    }


def get_receipt(root: Path, run_id: str) -> dict[str, Any]:
    """One receipt with the evidence it collected, or an error naming the run."""
    record = read_record(root, run_id)
    if record is None:
        return {
            "run_id": str(run_id),
            "found": False,
            "reason": f"no {REPORT_NAME} from this schema is readable under the evidence root",
        }
    return {
        "run_id": record.get("run_id"),
        "found": True,
        "receipt": {**view(record), "path": record["path"], "sha256": record["sha256"]},
        "crash": crash_view(record),
        "evidence": evidence_files(root, run_id, record),
    }


def _shown(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_DETAIL_CHARS:
        return value[:MAX_DETAIL_CHARS] + "…"
    return value


def compare(root: Path, first: str, second: str) -> dict[str, Any]:
    """Two receipts, field by field: what matched, what changed, what is missing.

    A field one side lacks is ``absent`` on that side, not a difference: a run
    that crashed has a blamed driver and a run that passed does not, and calling
    that "changed from X to nothing" would be a worse answer than saying the
    second run never had one.
    """
    records = {name: read_record(root, name) for name in (first, second)}
    missing = [name for name, record in records.items() if record is None]
    if missing:
        return {
            "compared": False,
            "reason": "no readable receipt for: " + ", ".join(missing),
            "missing": missing,
        }
    views = {name: view(record) for name, record in records.items()}
    same: list[str] = []
    different: list[dict[str, Any]] = []
    for field in COMPARED:
        left, right = views[first].get(field), views[second].get(field)
        if left is None and right is None:
            continue
        if left == right:
            same.append(field)
            continue
        different.append({
            "field": field,
            "first": _shown(left),
            "second": _shown(right),
            "absent_on": "both" if left is None and right is None else (
                "first" if left is None else ("second" if right is None else None)
            ),
        })
    return {
        "compared": True,
        "first": {**views[first], "path": records[first]["path"]},
        "second": {**views[second], "path": records[second]["path"]},
        "same": same,
        "different": different,
        "identical": not different,
    }


__all__ = [
    "COMPARED", "MAX_RECEIPTS", "compare", "crash_view", "evidence_files",
    "get_receipt", "list_receipts", "read_record", "view",
]
