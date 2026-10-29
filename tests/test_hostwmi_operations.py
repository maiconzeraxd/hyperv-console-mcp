"""Power operations that outlive their call, and the status that reports them.

There is no watcher thread: an operation keeps the object that carries it — the
provider's job for a save, the VM's own id for a shutdown — and the answer is
read when ``operation_status`` is asked.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from hyperv_console_mcp.hostwmi import WmiBackend, operations
from hyperv_console_mcp.hostwmi.connection import WmiFailure

VM_ID = "D5AD7D1D-5CB3-441F-A9D7-138640C8F61A"
JOB_PATH = f'\\\\HOST\\root\\virtualization\\v2:Msvm_ConcreteJob.InstanceID="{VM_ID}"'
OFF = 3
SAVED = 6


class FakeConnection:
    """Answers the VM's state and replays one job."""

    def __init__(self, *, state: int = 2, job: Any = None) -> None:
        self.state = state
        self.job = {"JobState": 4, "ErrorDescription": None} if job is None else job

    def query(self, wql: str, *, namespace: str = "") -> list[dict[str, Any]]:
        if "FROM Msvm_ComputerSystem" in wql:
            return [{"EnabledState": self.state}]
        return []

    def get(self, path: str, *, namespace: str = "") -> dict[str, Any]:
        if isinstance(self.job, Exception):
            raise self.job
        return dict(self.job)

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        raise AssertionError("status reads; it does not call methods")


@pytest.fixture(autouse=True)
def empty_registry() -> Any:
    operations.forget_all()
    yield
    operations.forget_all()


def begin_shutdown() -> str:
    return operations.begin(
        "shutdown", "NORTH-Test", vm_id=VM_ID, target_state=OFF
    )["operation_id"]


def begin_save() -> str:
    return operations.begin(
        "save", "NORTH-Test", vm_id=VM_ID, target_state=SAVED, job_path=JOB_PATH
    )["operation_id"]


def status(identifier: str, connection: FakeConnection | None = None) -> dict[str, Any]:
    return operations.status(connection or FakeConnection(), {"operation_id": identifier})


def test_the_identifier_is_the_bridge_hex_and_the_answer_its_shape() -> None:
    answer = operations.begin("save", "NORTH-Test", vm_id=VM_ID, target_state=SAVED)
    assert len(answer["operation_id"]) == 32
    assert answer["operation_id"] == answer["operation_id"].lower()
    assert set(answer) == {
        "operation_id", "state", "action", "vm", "succeeded", "error", "started_at"
    }
    assert (answer["state"], answer["succeeded"], answer["error"]) == ("running", None, None)
    assert answer["action"] == "save"
    assert answer["vm"] == "NORTH-Test"


def test_an_unknown_operation_is_answered_not_refused() -> None:
    answer = status("0" * 32)
    assert answer == {
        "operation_id": "0" * 32,
        "state": "unknown",
        "action": None,
        "vm": None,
        "succeeded": None,
        "error": None,
        "started_at": None,
    }


def test_a_finished_job_completes_the_operation() -> None:
    identifier = begin_save()
    answer = status(identifier, FakeConnection(job={"JobState": 7}))
    assert (answer["state"], answer["succeeded"], answer["error"]) == ("completed", True, None)


def test_a_running_job_leaves_the_operation_running() -> None:
    identifier = begin_save()
    answer = status(identifier, FakeConnection(job={"JobState": 4}))
    assert (answer["state"], answer["succeeded"]) == ("running", None)


@pytest.mark.parametrize(
    ("state", "wording"), [(8, "was terminated"), (9, "was killed"), (10, "raised an exception")]
)
def test_a_failed_job_carries_the_provider_description(state: int, wording: str) -> None:
    identifier = begin_save()
    answer = status(identifier, FakeConnection(job={"JobState": state, "ErrorDescription": "0x7B"}))
    assert (answer["state"], answer["succeeded"]) == ("failed", False)
    assert answer["error"] == f"The operation {wording}: 0x7B."


def test_a_job_that_cannot_be_read_is_a_failure_not_an_error() -> None:
    identifier = begin_save()
    answer = status(identifier, FakeConnection(job=WmiFailure("the job is gone")))
    assert (answer["state"], answer["succeeded"], answer["error"]) == (
        "failed", False, "the job is gone"
    )


def test_a_shutdown_completes_when_the_vm_reaches_off() -> None:
    identifier = begin_shutdown()
    answer = status(identifier, FakeConnection(state=OFF))
    assert (answer["state"], answer["succeeded"]) == ("completed", True)


def test_a_shutdown_in_progress_stays_running() -> None:
    identifier = begin_shutdown()
    assert status(identifier, FakeConnection(state=2))["state"] == "running"


def test_a_settled_operation_does_not_change_afterwards() -> None:
    """Once the VM has been shut down, starting it again must not un-settle it."""
    identifier = begin_shutdown()
    status(identifier, FakeConnection(state=OFF))
    answer = status(identifier, FakeConnection(state=2))
    assert (answer["state"], answer["succeeded"]) == ("completed", True)


def test_a_shutdown_that_never_reaches_the_target_fails_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = SimpleNamespace(monotonic=lambda: 100.0)
    monkeypatch.setattr(operations, "time", clock)
    identifier = begin_shutdown()
    clock.monotonic = lambda: 100.0 + operations.DEADLINE_SECONDS
    answer = status(identifier, FakeConnection(state=2))
    assert (answer["state"], answer["succeeded"]) == ("failed", False)
    assert answer["error"] == "The VM was still Running after 1800s."


def test_the_registry_keeps_the_last_fifty() -> None:
    for _ in range(operations._MAX_OPERATIONS + 10):
        begin_shutdown()
    assert len(operations._REGISTRY) == operations._MAX_OPERATIONS


def test_the_backend_serves_operation_status() -> None:
    identifier = begin_shutdown()
    backend = WmiBackend(connect=lambda: FakeConnection(state=OFF))
    answer = backend.invoke("operation_status", {"operation_id": identifier})
    assert answer["state"] == "completed"
