"""Crash triage: which dump is read, what kd says, and how it meets the host."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import BackendError
from hyperv_console_mcp.lab import crash

DUMP = b"MDMP" * 4


def entry(path: str, *, when: str | None = None, size: int = len(DUMP)) -> dict[str, Any]:
    item: dict[str, Any] = {
        "path": path, "size": size, "sha256": hashlib.sha256(DUMP).hexdigest(),
    }
    if when is not None:
        item["modified_utc"] = when
    return item


def evidence(*files: dict[str, Any]) -> dict[str, Any]:
    return {"collected": True, "files": list(files)}


def write_dump(root: Path, relative: str) -> Path:
    path = root / "run-1" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(DUMP)
    return path


def test_a_minidump_is_read_rather_than_the_kernel_dump(tmp_path: Path) -> None:
    """!analyze -v on MEMORY.DMP can read as much as the guest has RAM."""
    write_dump(tmp_path, "minidumps/new.dmp")
    write_dump(tmp_path, "MEMORY.DMP")
    chosen, candidates = crash.choose_dump(tmp_path, "run-1", evidence(
        entry("MEMORY.DMP", when="2026-10-29T12:00:00Z", size=8 * 1024**3),
        entry("minidumps/new.dmp", when="2026-10-29T11:00:00Z"),
    ))
    assert chosen is not None and chosen.name == "new.dmp"
    assert candidates == ["minidumps/new.dmp", "MEMORY.DMP"]


def test_the_newest_minidump_is_the_one_analyzed(tmp_path: Path) -> None:
    """The guest copies three dumps; only one of them is this run's crash."""
    write_dump(tmp_path, "minidumps/old.dmp")
    write_dump(tmp_path, "minidumps/new.dmp")
    chosen, _ = crash.choose_dump(tmp_path, "run-1", evidence(
        entry("minidumps/old.dmp", when="2026-10-29T09:00:00Z"),
        entry("minidumps/new.dmp", when="2026-10-29T12:00:00Z"),
    ))
    assert chosen is not None and chosen.name == "new.dmp"


def test_an_older_guest_without_timestamps_still_chooses_a_minidump(tmp_path: Path) -> None:
    write_dump(tmp_path, "minidumps/a.dmp")
    write_dump(tmp_path, "MEMORY.DMP")
    chosen, _ = crash.choose_dump(tmp_path, "run-1", evidence(
        entry("MEMORY.DMP"), entry("minidumps/a.dmp"),
    ))
    assert chosen is not None and chosen.name == "a.dmp"


def test_an_index_that_escapes_the_run_directory_is_ignored(tmp_path: Path) -> None:
    chosen, candidates = crash.choose_dump(tmp_path, "run-1", evidence(
        entry("../outside.dmp"), entry("/absolute.dmp"),
    ))
    assert chosen is None
    assert candidates == []


def test_a_dump_the_host_does_not_have_is_not_analyzed(tmp_path: Path) -> None:
    chosen, candidates = crash.choose_dump(
        tmp_path, "run-1", evidence(entry("minidumps/gone.dmp"))
    )
    assert chosen is None
    assert candidates == ["minidumps/gone.dmp"]


def test_kds_reading_is_kept_and_the_host_fills_what_it_cannot() -> None:
    merged = crash.merge(
        [{"bugcheck_code": "0x1e", "parameters": ["0x1", "0x2", "0x3", "0x4"]}],
        {"found": True, "bugcheck_code": "0x1e", "failure_bucket_id": "AV_MyDriver",
         "blamed_driver": "MyDriver.sys"},
    )
    assert merged["bugcheck_code"] == "0x1e"
    assert merged["bugcheck_parameters"] == ["0x1", "0x2", "0x3", "0x4"]
    assert merged["failure_bucket_id"] == "AV_MyDriver"
    assert merged["blamed_driver"] == "MyDriver.sys"
    assert merged["sources"] == ["kd", "host"]


def test_the_merged_view_says_whether_the_names_are_real() -> None:
    """A module plus an offset is not the same reading as a module and a function."""
    merged = crash.merge(
        None,
        {"found": True, "bugcheck_code": "0xd1", "blamed_driver": "MyDriver.sys",
         "symbols_resolved": False},
    )
    assert merged["symbols_resolved"] is False


def test_a_dump_kd_could_not_read_leaves_the_hosts_numbers_standing() -> None:
    merged = crash.merge(
        {"bugcheck_code": "0x9f", "parameters": ["0x3"]},
        {"found": False},
    )
    assert merged["bugcheck_code"] == "0x9f"
    assert merged["sources"] == ["host"]
    assert "failure_bucket_id" not in merged


class StubService:
    def __init__(self, root: Path, *, error: Exception | None = None) -> None:
        self.settings = Settings(evidence_root=root)
        self.error = error
        self.opened: list[str] = []

    def kd_dump_open(self, vm_name: str, dump_path: str, commands: Any = None) -> dict[str, Any]:
        self.opened.append(dump_path)
        if self.error is not None:
            raise self.error
        return {
            "output": "!analyze -v output",
            "duration_ms": 900,
            "report": {"found": True, "bugcheck_code": "0x1e",
                       "failure_bucket_id": "AV_MyDriver", "blamed_driver": "MyDriver.sys"},
        }


def report_with(dump: bool, **extra: Any) -> dict[str, Any]:
    files = [entry("minidumps/new.dmp", when="2026-10-29T12:00:00Z")] if dump else []
    return {"run_id": "run-1", "evidence": evidence(*files), **extra}


def test_triage_analyzes_the_dump_and_keeps_the_log(tmp_path: Path) -> None:
    write_dump(tmp_path, "minidumps/new.dmp")
    service = StubService(tmp_path)
    result = crash.triage(service, "NORTH-Test", report_with(True))
    assert result["analyzed"] is True
    assert service.opened and service.opened[0].endswith("new.dmp")
    assert result["bugcheck"]["failure_bucket_id"] == "AV_MyDriver"
    assert Path(result["kd_log"]).read_text(encoding="utf-8") == "!analyze -v output"


def test_triage_without_a_dump_says_so_instead_of_failing(tmp_path: Path) -> None:
    service = StubService(tmp_path)
    result = crash.triage(service, "NORTH-Test", report_with(False))
    assert result["analyzed"] is False
    assert result["reason"]
    assert service.opened == []


def test_a_debugger_that_already_analyzed_the_crash_is_not_asked_twice(tmp_path: Path) -> None:
    write_dump(tmp_path, "minidumps/new.dmp")
    service = StubService(tmp_path)
    analysis = {"found": True, "bugcheck_code": "0xd1", "blamed_driver": "Other.sys"}
    result = crash.triage(
        service, "NORTH-Test", report_with(True, debugger={"analysis": analysis})
    )
    assert result["source"] == "debugger"
    assert result["bugcheck"]["blamed_driver"] == "Other.sys"
    assert service.opened == []


def test_a_debugger_that_could_not_answer_does_not_stop_the_dump(tmp_path: Path) -> None:
    write_dump(tmp_path, "minidumps/new.dmp")
    service = StubService(tmp_path)
    result = crash.triage(
        service, "NORTH-Test", report_with(True, debugger={"analysis": {"found": False}})
    )
    assert result["analyzed"] is True
    assert service.opened


def test_kd_missing_is_reported_beside_the_hosts_record(tmp_path: Path) -> None:
    write_dump(tmp_path, "minidumps/new.dmp")
    service = StubService(tmp_path, error=BackendError("no kd.exe", code="kd_not_found"))
    result = crash.triage(service, "NORTH-Test", report_with(True))
    assert result["analyzed"] is False
    assert result["error"] == "kd_not_found"
    assert result["dump"].endswith("new.dmp")


def test_a_missing_evidence_root_is_not_a_crash_in_the_triage() -> None:
    service = SimpleNamespace(settings=Settings(evidence_root=Path("C:/nowhere")))
    result = crash.triage(service, "NORTH-Test", {"run_id": "run-1", "evidence": {}})
    assert result["analyzed"] is False
    assert result["dumps"] == []
