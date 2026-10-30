"""The record of one cycle, written down so two runs can be compared.

A cycle already returns everything it learned, and that answer is only as
durable as the transcript it was printed in. This writes the parts worth
comparing - the package and its manifest hash, the checkpoint, the outcome, the
crash reading and the evidence - as one versioned file beside the evidence, so
"the same test, three runs apart" is a file diff rather than a memory.

The file is written with sorted keys and one field per line: it is meant to be
read by an operator and by a diff, not only by a parser.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .transfer import canonical_json

REPORT_NAME = "lab-cycle.json"
SCHEMA = 1
MAX_ERROR_CHARS = 300
_RUN_ID = re.compile(r"[A-Za-z0-9_.-]+")


def _evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """The evidence as a set of hashes: what was collected, not when."""
    if not evidence.get("collected"):
        return {
            "collected": False,
            "reason": evidence.get("reason"),
            "error": evidence.get("error"),
        }
    files = [
        {"path": item.get("path"), "size": item.get("size"), "sha256": item.get("sha256")}
        for item in evidence.get("files") or []
        if isinstance(item, dict)
    ]
    return {
        "collected": True,
        "files": len(files),
        # One hash over the whole set: two runs whose evidence differs by a
        # single byte differ here, without either file having to be opened.
        "sha256": hashlib.sha256(canonical_json(files)).hexdigest(),
        "contents": files,
    }


def _crash(crash: Any) -> dict[str, Any] | None:
    if not isinstance(crash, dict):
        return None
    bugcheck = crash.get("bugcheck") if isinstance(crash.get("bugcheck"), dict) else {}
    summary = {
        "analyzed": crash.get("analyzed"),
        "dump": crash.get("dump"),
        "kd_log": crash.get("kd_log"),
        "bugcheck_code": bugcheck.get("bugcheck_code"),
        "bugcheck_str": bugcheck.get("bugcheck_str"),
        "bugcheck_parameters": bugcheck.get("bugcheck_parameters"),
        "failure_bucket_id": bugcheck.get("failure_bucket_id"),
        "default_bucket_id": bugcheck.get("default_bucket_id"),
        "blamed_driver": bugcheck.get("blamed_driver"),
        "symbol_name": bugcheck.get("symbol_name"),
        "symbols_resolved": bugcheck.get("symbols_resolved"),
        # The stack is kept for the same reason the code is: a receipt that
        # survives the transcript has to answer "where did it die" without the
        # dump being opened a second time.
        "top_frames": bugcheck.get("top_frames"),
        "sources": bugcheck.get("sources"),
    }
    return {key: value for key, value in summary.items() if value is not None}


def _debugger(debug: Any) -> dict[str, Any] | None:
    if not isinstance(debug, dict):
        return None
    symbols = debug.get("symbols") if isinstance(debug.get("symbols"), dict) else {}
    summary = {
        "transport": debug.get("transport"),
        "event": debug.get("event"),
        "resolved": symbols.get("resolved"),
        "unresolved": symbols.get("unresolved"),
        "all_loaded": symbols.get("all_loaded"),
    }
    return {key: value for key, value in summary.items() if value is not None}


def build(report: dict[str, Any]) -> dict[str, Any]:
    """One cycle, reduced to what a later run can be compared against."""
    verification = report.get("verification") if isinstance(report.get("verification"), dict) else {}
    checkpoint = report.get("checkpoint") if isinstance(report.get("checkpoint"), dict) else {}
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "vm": report.get("vm"),
        "run_id": report.get("run_id"),
        "package": {
            "package_id": report.get("package_id"),
            "manifest_sha256": report.get("manifest_sha256") or verification.get("manifest_sha256"),
            "verified": verification.get("verified"),
            "files_verified": verification.get("files_verified"),
        },
        "outcome": report.get("outcome"),
        "detail": report.get("detail"),
        "started_utc": report.get("started_utc"),
        "finished_utc": report.get("finished_utc"),
        "duration_seconds": report.get("duration_seconds"),
        "payload_executed": report.get("payload_executed"),
        "debugger": _debugger(report.get("debugger")),
        "crash": _crash(report.get("crash")),
        "evidence": _evidence(report.get("evidence") or {}),
    }
    if checkpoint:
        record["checkpoint"] = {
            "name": checkpoint.get("name"),
            "id": checkpoint.get("id"),
            "created": checkpoint.get("created"),
            "revert": report.get("revert"),
        }
    return record


def write(report: dict[str, Any], root: Path) -> dict[str, Any]:
    """Write the record beside the evidence, and never fail the cycle over it.

    The run report is the answer; this file is a copy of the parts of it worth
    keeping. A host that cannot write it still has the cycle's result.
    """
    run_id = str(report.get("run_id") or "")
    if not _RUN_ID.fullmatch(run_id):
        return {"schema": SCHEMA, "written": False, "error": "the run id is not a file name"}
    record = build(report)
    data = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
    try:
        directory = Path(root).expanduser().resolve() / run_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / REPORT_NAME
        path.write_bytes(data)
    except OSError as exc:
        return {"schema": SCHEMA, "written": False, "error": str(exc)[:MAX_ERROR_CHARS]}
    return {
        "schema": SCHEMA,
        "written": True,
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


__all__ = ["REPORT_NAME", "SCHEMA", "build", "write"]
