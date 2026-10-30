"""The per-cycle record: what it keeps, and where it lands."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from hyperv_console_mcp.lab import report as lab_report
from hyperv_console_mcp.lab.transfer import canonical_json


def sample(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "vm": "NORTH-Test",
        "run_id": "run-1",
        "package_id": "north.1",
        "manifest_sha256": "a" * 64,
        "outcome": "bugcheck",
        "detail": "the host recorded a guest crash during the run (bugcheck 0xd1)",
        "started_utc": "2026-10-29T12:00:00+00:00",
        "finished_utc": "2026-10-29T12:00:14+00:00",
        "duration_seconds": 14.2,
        "payload_executed": True,
        "verification": {"verified": True, "files_verified": 3},
        "checkpoint": {"name": "pre-run-1", "id": "1111", "created": True},
        "revert": {"restored": True, "checkpoint": "pre-run-1", "snapshot_id": "1111"},
        "debugger": {
            "transport": "kdnet", "event": "bugcheck",
            "symbols": {"resolved": ["MyDriver"], "unresolved": [], "all_loaded": True},
        },
        "crash": {
            "analyzed": True, "dump": "C:/Evidence/run-1/minidumps/new.dmp",
            "bugcheck": {
                "bugcheck_code": "0xd1", "failure_bucket_id": "AV_MyDriver",
                "blamed_driver": "MyDriver.sys", "symbols_resolved": True,
            },
        },
        "evidence": {
            "collected": True,
            "files": [{"path": "minidumps/new.dmp", "size": 8, "sha256": "b" * 64}],
        },
    }
    record.update(overrides)
    return record


def test_the_record_keeps_what_two_runs_are_compared_on() -> None:
    record = lab_report.build(sample())
    assert record["schema"] == lab_report.SCHEMA
    assert record["package"]["manifest_sha256"] == "a" * 64
    assert record["package"]["verified"] is True
    assert record["checkpoint"]["revert"]["restored"] is True
    assert record["crash"]["failure_bucket_id"] == "AV_MyDriver"
    assert record["crash"]["symbols_resolved"] is True
    assert record["debugger"]["all_loaded"] is True


def test_the_record_keeps_the_stack_a_triage_shows() -> None:
    """A receipt outlives the window that ran it, so it carries its own triage."""
    crash = {
        "analyzed": True, "dump": "C:/Evidence/run-1/minidumps/new.dmp",
        "kd_log": "C:/Evidence/run-1/kd-analyze.log",
        "bugcheck": {
            "bugcheck_code": "0xd1", "bugcheck_str": "DRIVER_IRQL_NOT_LESS_OR_EQUAL",
            "bugcheck_parameters": ["0x1", "0x2", "0x3", "0x4"],
            "failure_bucket_id": "AV_MyDriver", "blamed_driver": "MyDriver.sys",
            "symbols_resolved": True, "sources": ["kd", "host"],
            "top_frames": [{"frame": 0, "address": "fffff800`10000000",
                            "symbol": "MyDriver!Crash+0x42"}],
        },
    }
    record = lab_report.build(sample(crash=crash))
    kept = record["crash"]
    assert kept["bugcheck_str"] == "DRIVER_IRQL_NOT_LESS_OR_EQUAL"
    assert kept["top_frames"][0]["symbol"] == "MyDriver!Crash+0x42"
    assert kept["sources"] == ["kd", "host"]
    assert kept["kd_log"].endswith("kd-analyze.log")


def test_the_expected_digest_is_kept_even_when_the_package_did_not_verify() -> None:
    """A run that never executed still has to say what it was asked to run."""
    record = lab_report.build(sample(
        outcome="not_verified", payload_executed=False,
        verification={"verified": False, "reason": "the guest has no package"},
    ))
    assert record["package"]["verified"] is False
    assert record["package"]["manifest_sha256"] == "a" * 64


def test_evidence_is_one_hash_over_the_files_it_collected() -> None:
    record = lab_report.build(sample())
    files = sample()["evidence"]["files"]
    assert record["evidence"]["sha256"] == hashlib.sha256(canonical_json(files)).hexdigest()
    assert record["evidence"]["files"] == 1


def test_a_different_byte_in_the_evidence_changes_the_hash() -> None:
    first = lab_report.build(sample())["evidence"]["sha256"]
    changed = sample()
    changed["evidence"]["files"][0]["sha256"] = "c" * 64
    assert lab_report.build(changed)["evidence"]["sha256"] != first


def test_a_run_without_evidence_says_why() -> None:
    record = lab_report.build(sample(
        evidence={"collected": False, "reason": "the run did not execute a payload"}
    ))
    assert record["evidence"] == {
        "collected": False, "reason": "the run did not execute a payload", "error": None,
    }


def test_a_run_that_had_no_debugger_and_no_crash_has_neither() -> None:
    record = lab_report.build(sample(outcome="ok", debugger=None, crash=None))
    assert record["debugger"] is None
    assert record["crash"] is None


def test_the_record_lands_beside_the_evidence(tmp_path: Path) -> None:
    written = lab_report.write(sample(), tmp_path)
    assert written["written"] is True
    path = Path(written["path"])
    assert path == tmp_path / "run-1" / lab_report.REPORT_NAME
    assert written["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert json.loads(path.read_text(encoding="utf-8"))["outcome"] == "bugcheck"


def test_the_record_is_written_for_a_diff_not_for_a_parser(tmp_path: Path) -> None:
    lab_report.write(sample(), tmp_path)
    text = (tmp_path / "run-1" / lab_report.REPORT_NAME).read_text(encoding="utf-8")
    assert "\n  \"crash\"" in text


def test_a_run_id_that_is_not_a_file_name_is_refused(tmp_path: Path) -> None:
    written = lab_report.write(sample(run_id="../escape"), tmp_path)
    assert written["written"] is False
    assert not (tmp_path / "escape").exists()


def test_a_host_that_cannot_write_the_record_still_has_the_cycle(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    written = lab_report.write(sample(), blocker)
    assert written["written"] is False
    assert written["error"]
