"""The multi-VM matrix: one package, several VMs, one comparison.

The cycle itself has its own tests. What this covers is the part that only
exists when there are two VMs: the run id each row gets, the comparison the
window draws, and the rule that one VM refusing the run is a row rather than the
end of the matrix.
"""

from __future__ import annotations

from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import HyperVError, PolicyError, ValidationError
from hyperv_console_mcp.lab import matrix as lab_matrix
from hyperv_console_mcp.service import HyperVService

DIGEST = "a" * 64


class FakeHost:
    def invoke(self, operation: str, payload: dict[str, Any], *, timeout: float | None = None):
        return {"vm": payload.get("vm"), "ok": True}


def report(vm: str, *, outcome: str = "ok", **extra: Any) -> dict[str, Any]:
    """One cycle's answer, reduced to the fields the matrix reads."""
    return {
        "vm": vm,
        "package_id": "north.1",
        "run_id": f"run-1-{vm}",
        "outcome": outcome,
        "detail": "the guest runner finished",
        "duration_seconds": 61.5,
        "payload_executed": True,
        "crash": None,
        "debugger": None,
        "recording": None,
        "report_file": {"written": True, "path": f"C:/Evidence/run-1-{vm}/lab-cycle.json"},
        **extra,
    }


# -- the list ------------------------------------------------------------

def test_a_matrix_needs_two_distinct_vms() -> None:
    with pytest.raises(ValidationError, match="at least two"):
        lab_matrix.normalise_vms(["only-one"])
    with pytest.raises(ValidationError, match="twice"):
        lab_matrix.normalise_vms(["A", "a"])
    with pytest.raises(ValidationError, match="list of VM names"):
        lab_matrix.normalise_vms("A,B")
    with pytest.raises(ValidationError, match="at most"):
        lab_matrix.normalise_vms(["a", "b", "c", "d", "e"])


def test_each_row_gets_its_own_run_id() -> None:
    assert lab_matrix.run_id_for("run-1", "NORTH-Test") == "run-1-NORTH-Test"
    # A VM name that is not a directory name is made into one, never joined raw.
    assert lab_matrix.run_id_for("run-1", "lab/vm 2") == "run-1-lab-vm-2"
    assert "/" not in lab_matrix.run_id_for("run-1", "..\\escape")


# -- the comparison ------------------------------------------------------

def test_identical_rows_are_reported_as_identical() -> None:
    rows = [
        {"vm": "A", "outcome": "ok", "duration_seconds": 10.0, "bugcheck_code": None,
         "blamed_driver": None, "symbols_all_loaded": None, "recording": None},
        {"vm": "B", "outcome": "ok", "duration_seconds": 10.0, "bugcheck_code": None,
         "blamed_driver": None, "symbols_all_loaded": None, "recording": None},
    ]

    result = lab_matrix.compare(rows)

    assert result["same"] is True
    assert result["differs"] == []
    assert result["same_outcome"] is True


def test_one_different_outcome_is_the_finding() -> None:
    rows = [
        {"vm": "Win10", "outcome": "ok", "duration_seconds": 10.0, "bugcheck_code": None,
         "blamed_driver": None, "symbols_all_loaded": True, "recording": "console.gif"},
        {"vm": "Win11", "outcome": "bugcheck", "duration_seconds": 12.0,
         "bugcheck_code": "0xd1", "blamed_driver": "MyDriver.sys",
         "symbols_all_loaded": True, "recording": "console.gif"},
    ]

    result = lab_matrix.compare(rows)

    assert result["same"] is False
    assert set(result["differs"]) == {"outcome", "duration_seconds", "bugcheck_code", "blamed_driver"}
    assert result["same_outcome"] is False
    assert result["outcomes"] == {"Win10": "ok", "Win11": "bugcheck"}
    # Every compared field is reported, differing or not, so the table has rows.
    assert [item["field"] for item in result["fields"]] == list(lab_matrix.FIELDS)


# -- the fan-out ---------------------------------------------------------

def test_the_matrix_runs_every_vm_and_names_each_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str]] = []

    def fake_cycle(service: Any, vm_name: str, **kwargs: Any) -> dict[str, Any]:
        seen.append((vm_name, kwargs["run_id"]))
        return report(vm_name)

    monkeypatch.setattr(lab_matrix, "cycle", fake_cycle)

    result = lab_matrix.matrix(
        object(), ["Win10", "Win11"],
        package_id="north.1", run_id="run-1", expected_manifest_sha256=DIGEST,
    )

    assert seen == [("Win10", "run-1-Win10"), ("Win11", "run-1-Win11")]
    assert result["count"] == 2
    assert [row["vm"] for row in result["vms"]] == ["Win10", "Win11"]
    assert result["same_outcome"] is True
    assert all(row["receipt"] for row in result["vms"])


def test_a_vm_that_refuses_the_run_is_a_row_not_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One VM off or out of level leaves the other rows worth having."""
    def fake_cycle(service: Any, vm_name: str, **kwargs: Any) -> dict[str, Any]:
        if vm_name == "Off-VM":
            raise PolicyError("lab_run needs the VM Running; it is Off.")
        return report(vm_name)

    monkeypatch.setattr(lab_matrix, "cycle", fake_cycle)

    result = lab_matrix.matrix(
        object(), ["Win10", "Off-VM", "Win11"],
        package_id="north.1", run_id="run-1", expected_manifest_sha256=DIGEST,
    )

    rows = {row["vm"]: row for row in result["vms"]}
    assert rows["Off-VM"]["outcome"] is None
    assert rows["Off-VM"]["error"] == "not_authorized"
    assert "needs the VM Running" in str(rows["Off-VM"]["detail"])
    assert rows["Win10"]["outcome"] == "ok"
    assert rows["Win11"]["outcome"] == "ok"
    assert result["count"] == 3


def test_a_matrix_failure_that_is_not_about_the_vm_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only HyperVError is a row; a programming error must not be swallowed."""
    def fake_cycle(service: Any, vm_name: str, **kwargs: Any) -> dict[str, Any]:
        raise KeyError("bug")

    monkeypatch.setattr(lab_matrix, "cycle", fake_cycle)

    with pytest.raises(KeyError):
        lab_matrix.matrix(
            object(), ["Win10", "Win11"],
            package_id="north.1", run_id="run-1", expected_manifest_sha256=DIGEST,
        )


def test_the_progress_names_the_vm_that_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: list[str] = []

    def fake_cycle(service: Any, vm_name: str, **kwargs: Any) -> dict[str, Any]:
        kwargs["progress"](50, "executing the staged package in the guest")
        return report(vm_name)

    monkeypatch.setattr(lab_matrix, "cycle", fake_cycle)

    lab_matrix.matrix(
        object(), ["Win10", "Win11"],
        package_id="north.1", run_id="run-1", expected_manifest_sha256=DIGEST,
        progress=lambda percent, message: messages.append(message),
    )

    assert any(message.startswith("Win10: ") for message in messages)
    assert any(message.startswith("Win11: ") for message in messages)


# -- the service ---------------------------------------------------------

def host(**settings: Any) -> HyperVService:
    values = {
        "allowed_vms": frozenset({"A", "B"}),
        "evidence_root": "C:/Evidence",
        "allow_guest": True,
        **settings,
    }
    return HyperVService(FakeHost(), Settings(**values))


def test_the_service_refuses_a_matrix_outside_the_lab_profile() -> None:
    with pytest.raises(PolicyError, match="HYPERV_MCP_PROFILE=lab"):
        host(profile="safe").lab_matrix(
            ["A", "B"], "north.1", "run-1", DIGEST,
        )


def test_the_service_checks_every_vm_against_the_allowlist() -> None:
    service = host(profile="lab")
    with pytest.raises(HyperVError, match="is not allowed"):
        service.lab_matrix(["A", "OUTSIDE"], "north.1", "run-1", DIGEST)


def test_the_service_hands_the_matrix_its_own_run_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_matrix(service: Any, names: list[str], **kwargs: Any) -> dict[str, Any]:
        seen["names"] = names
        seen["kwargs"] = kwargs
        return {"count": len(names), "vms": []}

    monkeypatch.setattr(lab_matrix, "matrix", fake_matrix)

    result = host(profile="lab").lab_matrix(
        ["A", "B"], "north.1", "run-1", DIGEST, record=True, debugger="kdnet",
    )

    assert result["count"] == 2
    assert seen["names"] == ["A", "B"]
    assert seen["kwargs"]["run_id"] == "run-1"
    assert seen["kwargs"]["record"] is True
    assert seen["kwargs"]["debugger"] == "kdnet"
