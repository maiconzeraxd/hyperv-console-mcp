"""Power transitions through RequestStateChange, and the job it hands back.

The two things worth pinning here are the numbers: the provider takes only the
CIM ``RequestedState`` values, and every refusal is one of its own 3276x codes.
Both were read off the live provider, and both had a wrong guess first.
"""

from __future__ import annotations

from typing import Any

import pytest

from hyperv_console_mcp.hostwmi import WmiBackend
from hyperv_console_mcp.hostwmi.connection import WmiFailure
from hyperv_console_mcp.hostwmi.power import (
    JOB_FAILURES,
    REQUESTED_STATES,
    RETURN_CODES,
    await_job,
    vm_power,
)

VM_ID = "D5AD7D1D-5CB3-441F-A9D7-138640C8F61A"
JOB_PATH = f'\\\\HOST\\root\\virtualization\\v2:Msvm_ConcreteJob.InstanceID="{VM_ID}"'


class FakeConnection:
    """Answers the VM queries, records method calls, replays a job's states."""

    def __init__(
        self,
        *,
        response: dict[str, Any] | None = None,
        jobs: list[int] | None = None,
        description: str = "no detail",
        enabled_state: int = 2,
    ) -> None:
        self.response = {"ReturnValue": 0} if response is None else response
        self.jobs = list(jobs or [])
        self.description = description
        self.enabled_state = enabled_state
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[str] = []

    def query(self, wql: str, *, namespace: str = "") -> list[dict[str, Any]]:
        if "FROM Msvm_ComputerSystem" in wql:
            return [
                {
                    "ElementName": "NORTH-Test",
                    "Name": VM_ID,
                    "EnabledState": self.enabled_state,
                    "StatusDescriptions": ["Operando normalmente"],
                    "OnTimeInMilliseconds": 1000,
                    "__PATH": f'\\\\HOST\\root\\virtualization\\v2:Msvm_ComputerSystem.Name="{VM_ID}"',
                }
            ]
        if "System:Realized" in wql:
            return [
                {
                    "ElementName": "NORTH-Test",
                    "Version": "9.0",
                    "VirtualSystemSubType": "Microsoft:Hyper-V:SubType:2",
                    "UserSnapshotType": 5,
                    "AutomaticSnapshotsEnabled": False,
                    "AutomaticStartupAction": 2,
                    "AutomaticShutdownAction": 3,
                }
            ]
        return []

    def get(self, path: str, *, namespace: str = "") -> dict[str, Any]:
        self.gets.append(path)
        # Hold the last state, so a short list still answers every poll.
        state = self.jobs.pop(0) if len(self.jobs) > 1 else self.jobs[0]
        return {"JobState": state, "ErrorDescription": self.description}

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        self.calls.append((method, parameters))
        return self.response


@pytest.mark.parametrize(
    ("action", "requested"),
    [("start", 2), ("turn_off", 3), ("pause", 9), ("reset", 11), ("resume", 2)],
)
def test_each_action_asks_for_its_documented_state(action: str, requested: int) -> None:
    connection = FakeConnection()
    vm_power(connection, {"vm": "NORTH-Test", "action": action})
    assert connection.calls == [("RequestStateChange", {"RequestedState": requested})]


def test_resume_asks_for_enabled_not_for_resuming() -> None:
    """32777 is the Resuming *EnabledState*; as a RequestedState the provider
    refuses it with 32775. Resuming is the same request as starting."""
    assert REQUESTED_STATES["resume"] == 2
    assert 32777 not in REQUESTED_STATES.values()


def test_a_completed_transition_answers_the_bridge_shape() -> None:
    connection = FakeConnection(enabled_state=9)
    result = vm_power(connection, {"vm": "NORTH-Test", "action": "pause"})
    assert set(result) == {"accepted", "operation_id", "action", "state", "started_at", "vm"}
    assert result["accepted"] is False
    assert result["operation_id"] is None
    assert result["action"] == "pause"
    assert result["state"] == "completed"
    assert result["started_at"] is None
    assert (result["vm"]["state"], result["vm"]["state_code"]) == ("Paused", 9)
    assert connection.gets == []


def test_a_started_job_is_polled_until_it_completes() -> None:
    connection = FakeConnection(response={"ReturnValue": 4096, "Job": JOB_PATH}, jobs=[2, 2, 7])
    vm_power(connection, {"vm": "NORTH-Test", "action": "pause"})
    assert connection.gets == [JOB_PATH, JOB_PATH, JOB_PATH]


def test_a_failed_job_carries_the_provider_description() -> None:
    connection = FakeConnection(
        response={"ReturnValue": 4096, "Job": JOB_PATH}, jobs=[10], description="0x0000007B"
    )
    with pytest.raises(WmiFailure) as caught:
        vm_power(connection, {"vm": "NORTH-Test", "action": "pause"})
    assert str(caught.value) == "The transition raised an exception: 0x0000007B."


@pytest.mark.parametrize(("state", "wording"), sorted(JOB_FAILURES.items()))
def test_each_job_failure_state_is_named(state: int, wording: str) -> None:
    connection = FakeConnection(response={"ReturnValue": 4096, "Job": JOB_PATH}, jobs=[state])
    with pytest.raises(WmiFailure) as caught:
        vm_power(connection, {"vm": "NORTH-Test", "action": "pause"})
    assert str(caught.value) == f"The transition {wording}: no detail."


@pytest.mark.parametrize(("code", "expected"), sorted(RETURN_CODES.items()))
def test_every_documented_refusal_becomes_its_own_error(code: int, expected: tuple[str, str]) -> None:
    connection = FakeConnection(response={"ReturnValue": code})
    with pytest.raises(WmiFailure) as caught:
        vm_power(connection, {"vm": "NORTH-Test", "action": "pause"})
    name, description = expected
    assert caught.value.code == name
    assert str(caught.value) == f"The provider refused the transition with code {code}: {description}."


def test_an_undocumented_refusal_keeps_the_number() -> None:
    connection = FakeConnection(response={"ReturnValue": 12345})
    with pytest.raises(WmiFailure) as caught:
        vm_power(connection, {"vm": "NORTH-Test", "action": "pause"})
    assert caught.value.code == "backend_error"
    assert str(caught.value) == "The provider refused the transition with code 12345."


def test_a_job_that_does_not_finish_times_out() -> None:
    connection = FakeConnection(response={"ReturnValue": 4096, "Job": JOB_PATH}, jobs=[2])
    with pytest.raises(WmiFailure) as caught:
        await_job(connection, {"ReturnValue": 4096, "Job": JOB_PATH}, timeout=0)
    assert caught.value.code == "timeout"


def test_a_running_job_that_names_no_path_is_an_error() -> None:
    connection = FakeConnection(response={"ReturnValue": 4096})
    with pytest.raises(WmiFailure) as caught:
        vm_power(connection, {"vm": "NORTH-Test", "action": "pause"})
    assert str(caught.value) == "The provider reported a running job but named none."


@pytest.mark.parametrize("action", ["shutdown", "save"])
def test_background_actions_stay_on_the_bridge(action: str) -> None:
    connection = FakeConnection()
    with pytest.raises(WmiFailure) as caught:
        vm_power(connection, {"vm": "NORTH-Test", "action": action})
    assert caught.value.code == "wmi_unsupported"
    assert connection.calls == []


def test_an_unknown_action_is_refused_before_any_call() -> None:
    connection = FakeConnection()
    with pytest.raises(WmiFailure) as caught:
        vm_power(connection, {"vm": "NORTH-Test", "action": "teleport"})
    assert caught.value.code == "invalid_argument"
    assert str(caught.value) == "Unsupported power action 'teleport'."
    assert connection.calls == []


def test_the_backend_serves_vm_power() -> None:
    backend = WmiBackend(connect=lambda: FakeConnection(enabled_state=9))
    result = backend.invoke("vm_power", {"vm": "NORTH-Test", "action": "pause"})
    assert result["vm"]["state"] == "Paused"
