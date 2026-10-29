"""What a crash left behind: the dump, kd's reading of it, and the host's record.

``lab_cycle`` already collects the guest's minidumps, and the host already
records the bugcheck as it happens. This turns the two into one report: kd runs
``!analyze -v`` against the collected dump on the host, which is where
``FAILURE_BUCKET_ID`` and the blamed module come from when the run had no
debugger attached. Without that, a crash without a debugger said only that the
guest had bugchecked and left the operator to open the dump by hand.

A dump the guest did not produce is not an error for the run: the report keeps
the host's code and parameters and says why kd could not add to them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ..errors import HyperVError

KD_LOG_NAME = "kd-analyze.log"


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _parts(value: Any) -> list[str] | None:
    """The relative path an evidence entry names, or None if it could escape."""
    text = str(value or "").replace("\\", "/")
    if not text:
        return None
    parts = PurePosixPath(text).parts
    if not parts or any(part in ("..", "/") or part.startswith("/") for part in parts):
        return None
    return [part for part in parts if part not in ("", ".")]


def choose_dump(
    root: Path, run_id: str, evidence: dict[str, Any]
) -> tuple[Path | None, list[str]]:
    """The dump worth analyzing: the newest minidump, else the kernel dump.

    A minidump is a few megabytes and ``!analyze -v`` reads it in seconds, where
    ``MEMORY.DMP`` can be as large as the guest's RAM. The guest stamps each
    file it collected, so "newest" is the crash this run caused rather than
    whichever name sorts last.
    """
    entries = [entry for entry in evidence.get("files") or [] if isinstance(entry, dict)]
    dumps: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for entry in entries:
        parts = _parts(entry.get("path"))
        if parts is None or not parts[-1].casefold().endswith(".dmp"):
            continue
        minidump = len(parts) >= 2 and parts[-2].casefold() == "minidumps"
        when = _parse_time(entry.get("modified_utc"))
        size = entry.get("size")
        size = size if isinstance(size, int) and not isinstance(size, bool) else 0
        dumps.append(((minidump, when or datetime.min.replace(tzinfo=timezone.utc), -size), entry))
    if not dumps:
        return None, []
    dumps.sort(key=lambda item: item[0], reverse=True)
    candidates = [str(entry.get("path")) for _, entry in dumps]
    parts = _parts(dumps[0][1].get("path"))
    if parts is None:
        return None, candidates
    path = root / run_id
    for part in parts:
        path = path / part
    return (path if path.is_file() else None), candidates


def merge(host: Any, analysis: dict[str, Any]) -> dict[str, Any]:
    """One view of the crash: kd's reading, with the host's numbers beside it.

    kd is what names the bucket and the module; the host event is recorded while
    the crash is happening, so its code and parameters survive a dump that kd
    could not read. Nothing is invented: a field neither source has is absent.
    """
    event: dict[str, Any] = {}
    if isinstance(host, list):
        events = [item for item in host if isinstance(item, dict)]
        event = events[-1] if events else {}
    elif isinstance(host, dict):
        event = host
    merged = {
        "bugcheck_code": analysis.get("bugcheck_code") or event.get("bugcheck_code"),
        "bugcheck_parameters": (
            analysis.get("bugcheck_parameters") or event.get("parameters") or None
        ),
        "bugcheck_str": analysis.get("bugcheck_str"),
        "failure_bucket_id": analysis.get("failure_bucket_id"),
        "default_bucket_id": analysis.get("default_bucket_id"),
        "blamed_driver": analysis.get("blamed_driver") or analysis.get("module_name"),
        "symbol_name": analysis.get("symbol_name"),
        "top_frames": analysis.get("top_frames"),
        "host_event": event or None,
        "sources": [
            name
            for name, present in (("kd", bool(analysis.get("found"))), ("host", bool(event)))
            if present
        ],
    }
    return {key: value for key, value in merged.items() if value is not None}


def triage(service: Any, vm_name: str, report: dict[str, Any]) -> dict[str, Any]:
    """Analyze the dump this run collected, and merge it with the host's record."""
    existing = (report.get("debugger") or {}).get("analysis") or {}
    if isinstance(existing, dict) and existing.get("found"):
        # kd was attached during the run and already read the live target.
        return {
            "analyzed": True,
            "source": "debugger",
            "report": existing,
            "bugcheck": merge(report.get("bugcheck"), existing),
        }
    root = Path(service.settings.evidence_root).expanduser().resolve()
    run_id = str(report.get("run_id") or "")
    dump, candidates = choose_dump(root, run_id, report.get("evidence") or {})
    result: dict[str, Any] = {
        "analyzed": False,
        "dump": str(dump) if dump is not None else None,
        "dumps": candidates,
    }
    if dump is None:
        result["reason"] = "the run collected no dump to analyze"
        return result
    try:
        opened = service.kd_dump_open(vm_name, str(dump))
    except HyperVError as exc:
        result.update({"error": exc.code, "message": str(exc)})
        return result
    analysis = opened.get("report") or {}
    log = _write_log(root / run_id, str(opened.get("output") or ""))
    result.update({
        "analyzed": bool(analysis.get("found")),
        "kd_log": str(log) if log is not None else None,
        "duration_ms": opened.get("duration_ms"),
        "report": analysis,
        "bugcheck": merge(report.get("bugcheck"), analysis),
    })
    return result


def _write_log(directory: Path, text: str) -> Path | None:
    """Keep kd's own output beside the evidence, so the reading can be checked."""
    if not text:
        return None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / KD_LOG_NAME
        path.write_text(text, encoding="utf-8")
    except OSError:
        return None
    return path


__all__ = ["KD_LOG_NAME", "choose_dump", "merge", "triage"]
