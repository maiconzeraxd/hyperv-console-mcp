"""Receipts: what a cycle left behind, and what changed between two runs.

The record itself is `lab.report`'s job and has its own tests. This covers the
read side the window uses: listing what the evidence root holds, opening one
run's evidence, and comparing two runs field by field — including the cases
where a directory holds no readable record, because "there is no receipt" is an
answer and a guess is not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import ValidationError
from hyperv_console_mcp.lab import receipts
from hyperv_console_mcp.lab.report import REPORT_NAME, write
from hyperv_console_mcp.service import HyperVService

VM = "NORTH-Test"


def report(
    run_id: str,
    *,
    outcome: str = "ok",
    detail: str = "the guest runner finished and reported success",
    bugcheck: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    debugger: dict[str, Any] | None = None,
    checkpoint: str | None = None,
    finished: str = "2026-10-29T10:00:00+00:00",
) -> dict[str, Any]:
    """One cycle report, shaped as ``lab.run`` returns it."""
    return {
        "vm": VM,
        "package_id": "north.1",
        "run_id": run_id,
        "manifest_sha256": "a" * 64,
        "outcome": outcome,
        "detail": detail,
        "started_utc": "2026-10-29T09:59:00+00:00",
        "finished_utc": finished,
        "duration_seconds": 61.5,
        "payload_executed": True,
        "verification": {"verified": True, "files_verified": 3, "manifest_sha256": "a" * 64},
        "checkpoint": {"name": checkpoint or f"pre-{run_id}", "id": "1111", "created": True},
        "revert": None,
        "debugger": debugger,
        "crash": {"analyzed": True, "bugcheck": bugcheck} if bugcheck else None,
        "evidence": evidence
        or {
            "collected": True,
            "files": [
                {"path": "run.log", "size": 12, "sha256": "b" * 64},
                {"path": "Minidumps/102926-01.dmp", "size": 4096, "sha256": "c" * 64},
            ],
        },
    }


def written(root: Path, run_id: str, **kwargs: Any) -> dict[str, Any]:
    result = write(report(run_id, **kwargs), root)
    assert result["written"] is True, result
    return result


@pytest.fixture
def evidence(tmp_path: Path) -> Path:
    root = tmp_path / "evidence"
    root.mkdir()
    return root


# -- listing -------------------------------------------------------------

def test_a_run_is_listed_with_the_fields_two_runs_are_compared_on(evidence: Path) -> None:
    written(evidence, "run-1")
    written(evidence, "run-2", outcome="bugcheck", bugcheck={
        "analyzed": True,
        "bugcheck_code": "0xd1",
        "failure_bucket_id": "AV_MyDriver!Crash",
        "blamed_driver": "MyDriver.sys",
        "symbols_resolved": True,
    }, finished="2026-10-29T11:00:00+00:00")

    listed = receipts.list_receipts(evidence)

    assert listed["count"] == 2
    # Newest first: the crash is the one an operator is looking for.
    assert [item["run_id"] for item in listed["receipts"]] == ["run-2", "run-1"]
    crash = listed["receipts"][0]
    assert crash["outcome"] == "bugcheck"
    assert crash["bugcheck_code"] == "0xd1"
    assert crash["blamed_driver"] == "MyDriver.sys"
    assert crash["package_id"] == "north.1"
    assert crash["evidence_files"] == 2
    assert crash["verified"] is True
    assert Path(crash["path"]).name == REPORT_NAME


def test_a_receipt_can_be_limited_to_one_vm(evidence: Path) -> None:
    written(evidence, "run-1")
    other = report("run-2")
    other["vm"] = "OTHER-Test"
    write(other, evidence)

    assert receipts.list_receipts(evidence, vm_name=VM)["count"] == 1
    assert receipts.list_receipts(evidence, vm_name="other-test")["count"] == 1
    assert receipts.list_receipts(evidence, vm_name=None)["count"] == 2


def test_a_directory_without_a_readable_record_is_skipped_not_guessed(
    evidence: Path,
) -> None:
    written(evidence, "good")
    (evidence / "empty").mkdir()
    (evidence / "broken").mkdir()
    (evidence / "broken" / REPORT_NAME).write_text("{not json", encoding="utf-8")
    (evidence / "future").mkdir()
    (evidence / "future" / REPORT_NAME).write_text(
        json.dumps({"schema": 99, "run_id": "future"}), encoding="utf-8"
    )

    listed = receipts.list_receipts(evidence)

    assert [item["run_id"] for item in listed["receipts"]] == ["good"]
    assert sorted(listed["skipped"]) == ["broken", "empty", "future"]


def test_a_run_id_that_is_a_path_is_refused(evidence: Path) -> None:
    for name in ("..", "../etc", r"..\windows", "", "a/b"):
        with pytest.raises(ValidationError, match="plain directory name"):
            receipts.get_receipt(evidence, name)


def test_a_missing_evidence_root_lists_nothing_rather_than_failing(
    tmp_path: Path,
) -> None:
    listed = receipts.list_receipts(tmp_path / "not-there")
    assert listed["count"] == 0
    assert listed["receipts"] == []


# -- one receipt ---------------------------------------------------------

def test_opening_a_receipt_names_where_each_evidence_file_is(evidence: Path) -> None:
    written(evidence, "run-1")

    found = receipts.get_receipt(evidence, "run-1")

    assert found["found"] is True
    assert found["receipt"]["outcome"] == "ok"
    files = {item["path"]: item for item in found["evidence"]}
    assert set(files) == {"run.log", "Minidumps/102926-01.dmp"}
    # The window hands the operator a place to look, not a name to search for.
    assert Path(files["run.log"]["host_path"]) == evidence / "run-1" / "run.log"
    assert files["run.log"]["sha256"] == "b" * 64


def test_an_evidence_entry_that_climbs_out_of_the_run_is_not_listed(
    evidence: Path,
) -> None:
    written(evidence, "run-1", evidence={
        "collected": True,
        "files": [
            {"path": "../../secret.txt", "size": 1, "sha256": "d" * 64},
            {"path": "run.log", "size": 12, "sha256": "b" * 64},
        ],
    })

    found = receipts.get_receipt(evidence, "run-1")

    assert [item["path"] for item in found["evidence"]] == ["run.log"]


def test_a_receipt_carries_the_crash_a_later_window_can_triage(evidence: Path) -> None:
    """Opening a run is enough to show the triage: no second kd run, no dump read."""
    written(evidence, "run-1", outcome="bugcheck", bugcheck={
        "analyzed": True,
        "bugcheck_code": "0xd1",
        "bugcheck_str": "DRIVER_IRQL_NOT_LESS_OR_EQUAL",
        "bugcheck_parameters": ["0x1", "0x2", "0x3", "0x4"],
        "failure_bucket_id": "AV_MyDriver!Crash",
        "blamed_driver": "MyDriver.sys",
        "symbols_resolved": True,
        "sources": ["kd", "host"],
        "top_frames": [{"frame": 0, "address": "fffff800`10000000", "symbol": "MyDriver!Crash+0x42"}],
    })

    found = receipts.get_receipt(evidence, "run-1")

    crash = found["crash"]
    assert crash["bugcheck_code"] == "0xd1"
    assert crash["bugcheck_str"] == "DRIVER_IRQL_NOT_LESS_OR_EQUAL"
    assert crash["blamed_driver"] == "MyDriver.sys"
    assert crash["top_frames"][0]["symbol"] == "MyDriver!Crash+0x42"


def test_a_run_that_did_not_crash_has_no_triage(evidence: Path) -> None:
    written(evidence, "run-1")
    assert receipts.get_receipt(evidence, "run-1")["crash"] is None


def test_opening_a_run_without_a_record_says_so(evidence: Path) -> None:
    found = receipts.get_receipt(evidence, "never-ran")
    assert found == {
        "run_id": "never-ran",
        "found": False,
        "reason": found["reason"],
    }
    assert "no lab-cycle.json" in found["reason"]


# -- comparing two -------------------------------------------------------

def test_comparing_two_runs_names_what_changed(evidence: Path) -> None:
    written(evidence, "run-1")
    written(evidence, "run-2", outcome="bugcheck", bugcheck={
        "analyzed": True, "bugcheck_code": "0xd1", "blamed_driver": "MyDriver.sys",
    })

    compared = receipts.compare(evidence, "run-1", "run-2")

    assert compared["compared"] is True
    assert compared["identical"] is False
    changed = {item["field"]: item for item in compared["different"]}
    assert changed["outcome"]["first"] == "ok"
    assert changed["outcome"]["second"] == "bugcheck"
    assert changed["bugcheck_code"]["first"] is None
    # A field only the crashing run has is absent on the other, not "changed to
    # nothing": that distinction is the whole reason the comparison says it.
    assert changed["bugcheck_code"]["absent_on"] == "first"
    # The package and its digest are the same in both, which is what makes the
    # two runs comparable at all.
    assert {"package_id", "manifest_sha256", "verified"} <= set(compared["same"])


def test_two_runs_of_the_same_record_are_identical(evidence: Path) -> None:
    # The checkpoint is named after the run, so two runs of the same package
    # differ there unless the name is held fixed; everything else is identical.
    written(evidence, "run-1", checkpoint="pre-baseline")
    written(evidence, "run-2", checkpoint="pre-baseline")

    compared = receipts.compare(evidence, "run-1", "run-2")

    assert compared["identical"] is True
    assert compared["different"] == []
    assert "outcome" in compared["same"]


def test_comparing_with_a_run_that_has_no_receipt_says_which(
    evidence: Path,
) -> None:
    written(evidence, "run-1")

    compared = receipts.compare(evidence, "run-1", "ghost")

    assert compared["compared"] is False
    assert compared["missing"] == ["ghost"]
    assert "ghost" in compared["reason"]


def test_the_evidence_hash_is_compared_too(evidence: Path) -> None:
    """Two runs can agree on the outcome and still have collected different files."""
    written(evidence, "run-1")
    written(evidence, "run-2", evidence={
        "collected": True,
        "files": [{"path": "run.log", "size": 13, "sha256": "e" * 64}],
    })

    compared = receipts.compare(evidence, "run-1", "run-2")
    changed = {item["field"] for item in compared["different"]}

    assert {"evidence_files", "evidence_sha256"} <= changed


# -- the service ---------------------------------------------------------

class FakeHost:
    """A backend that is never reached: receipts are read on the host."""

    def invoke(self, operation: str, payload: Any, *, timeout: Any = None) -> Any:
        raise AssertionError(f"the host backend must not be called for {operation}")


def service(evidence: Path) -> HyperVService:
    return HyperVService(
        FakeHost(), Settings(allowed_vms=frozenset({VM}), evidence_root=evidence)
    )


def test_the_service_lists_receipts_and_refuses_a_bad_action(evidence: Path) -> None:
    written(evidence, "run-1")
    host = service(evidence)

    assert host.lab_receipts("list")["count"] == 1
    assert host.lab_receipts("get", ["run-1"])["found"] is True
    assert host.lab_receipts("compare", ["run-1", "run-1"])["identical"] is True
    with pytest.raises(ValidationError, match="list, get, or compare"):
        host.lab_receipts("delete")


def test_the_service_needs_the_right_number_of_run_ids(evidence: Path) -> None:
    host = service(evidence)
    with pytest.raises(ValidationError, match="exactly one run id"):
        host.lab_receipts("get", ["a", "b"])
    with pytest.raises(ValidationError, match="exactly two run ids"):
        host.lab_receipts("compare", ["a"])


def test_the_service_keeps_a_receipt_read_inside_the_allowlist(
    evidence: Path, tmp_path: Path
) -> None:
    written(evidence, "run-1")
    host = service(evidence)

    with pytest.raises(Exception, match="is not allowed"):
        host.lab_receipts("list", vm_name="OUTSIDE-Test")
