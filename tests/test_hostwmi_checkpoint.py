"""Checkpoints read from the snapshot setting data, in Get-VMSnapshot's shape.

The rows are the ones this host answers with: the ``InstanceID`` is
``Microsoft:<snapshot id>``, ``Parent`` is the previous checkpoint's reference,
and ``CreationTime`` is a CIM DATETIME in UTC.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import pytest

from hyperv_console_mcp.errors import BackendError
from hyperv_console_mcp.hostwmi import WmiBackend
from hyperv_console_mcp.hostwmi.checkpoint import (
    checkpoint_delete,
    checkpoint_list,
    checkpoint_restore,
)
from hyperv_console_mcp.hostwmi.connection import WmiFailure

VM_ID = "D5AD7D1D-5CB3-441F-A9D7-138640C8F61A"
FIRST = "0377C557-9387-4502-B43C-C373EFD8F88A"
SECOND = "1551D234-08A6-45F7-9AAA-D4F57389DDBD"
SERVICE_PATH = (
    r"\\HOST\root\virtualization\v2:Msvm_VirtualSystemSnapshotService."
    'CreationClassName="Msvm_VirtualSystemSnapshotService",Name="vssnapsvc",'
    'SystemCreationClassName="Msvm_ComputerSystem",SystemName="HOST"'
)
ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}[+-]\d{2}:\d{2}")


class FakeConnection:
    """Answers the three queries the checkpoint code makes, and records calls."""

    def __init__(
        self,
        snapshots: list[dict[str, Any]] | None = None,
        *,
        has_service: bool = True,
        response: dict[str, Any] | None = None,
        jobs: list[int] | None = None,
    ) -> None:
        self.snapshots = snapshots if snapshots is not None else _rows()
        self.has_service = has_service
        self.response = {"ReturnValue": 0} if response is None else response
        self.jobs = list(jobs or [])
        self.queries: list[str] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def query(self, wql: str, *, namespace: str = "") -> list[dict[str, Any]]:
        self.queries.append(wql)
        if "FROM Msvm_ComputerSystem" in wql:
            return [
                {
                    "ElementName": "NORTH-Test",
                    "Name": VM_ID,
                    "EnabledState": 2,
                    "StatusDescriptions": [],
                    "OnTimeInMilliseconds": 0,
                    "__PATH": f'\\\\HOST\\root\\virtualization\\v2:Msvm_ComputerSystem.Name="{VM_ID}"',
                }
            ]
        if "FROM Msvm_VirtualSystemSnapshotService" in wql:
            return [{"__PATH": SERVICE_PATH}] if self.has_service else []
        if "Snapshot:Realized" in wql:
            return list(self.snapshots)
        raise AssertionError(f"unexpected query: {wql}")

    def get(self, path: str, *, namespace: str = "") -> dict[str, Any]:
        state = self.jobs.pop(0) if len(self.jobs) > 1 else self.jobs[0]
        return {"JobState": state}

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        self.calls.append((path, method, parameters))
        return self.response


def _row(
    identifier: str,
    name: str,
    created: str,
    *,
    parent: str | None = None,
    notes: Any = ("Isolated NORTH driver research VM.",),
) -> dict[str, Any]:
    return {
        "InstanceID": f"Microsoft:{identifier}",
        "ElementName": name,
        "CreationTime": created,
        "Parent": (
            '\\\\HOST\\root\\virtualization\\v2:Msvm_VirtualSystemSettingData.InstanceID='
            f'"Microsoft:{parent}"'
            if parent
            else None
        ),
        "Notes": notes,
        "__PATH": (
            '\\\\HOST\\root\\virtualization\\v2:Msvm_VirtualSystemSettingData.InstanceID='
            f'"Microsoft:{identifier}"'
        ),
    }


def _rows() -> list[dict[str, Any]]:
    # Out of order on purpose: the mapper sorts them oldest first.
    return [
        _row(SECOND, "Before-NORTH-detailed-logging-2026-10-28", "20261029021805.014730-000", parent=FIRST),
        _row(FIRST, "Before-NORTH-live-2026-10-28", "20261029014417.778334-000"),
    ]


def test_checkpoint_list_maps_the_snapshot_rows_oldest_first() -> None:
    result = checkpoint_list(FakeConnection(), {"vm": "NORTH-Test"})
    assert result["vm"] == "NORTH-Test"
    assert result["count"] == 2
    assert [row["name"] for row in result["checkpoints"]] == [
        "Before-NORTH-live-2026-10-28",
        "Before-NORTH-detailed-logging-2026-10-28",
    ]


def test_each_checkpoint_has_the_shape_get_vmsnapshot_answers() -> None:
    first, second = checkpoint_list(FakeConnection(), {"vm": "NORTH-Test"})["checkpoints"]
    assert first["id"] == FIRST.lower()
    assert first["parent_snapshot_id"] is None
    assert first["notes"] == "Isolated NORTH driver research VM."
    assert second["parent_snapshot_id"] == FIRST.lower()
    assert set(first) == {"id", "name", "creation_time", "parent_snapshot_id", "notes"}


def test_creation_time_is_the_same_instant_in_the_bridge_format() -> None:
    first = checkpoint_list(FakeConnection(), {"vm": "NORTH-Test"})["checkpoints"][0]
    assert ISO.fullmatch(first["creation_time"])
    # Seven fractional digits: the bridge writes round-trip precision.
    assert ".7783340" in first["creation_time"]
    assert datetime.fromisoformat(first["creation_time"]) == datetime(
        2026, 10, 29, 1, 44, 17, 778334, tzinfo=timezone.utc
    )


def test_a_cim_offset_is_read_in_minutes_not_as_a_clock_shift() -> None:
    row = _row(FIRST, "s", "20261029014417.778334+120")
    listed = checkpoint_list(FakeConnection([row]), {"vm": "NORTH-Test"})["checkpoints"][0]
    assert datetime.fromisoformat(listed["creation_time"]) == datetime(
        2026, 10, 28, 23, 44, 17, 778334, tzinfo=timezone.utc
    )


def test_notes_that_arrive_as_a_string_are_left_alone() -> None:
    row = _row(FIRST, "s", "20261029014417.778334-000", notes="one line")
    listed = checkpoint_list(FakeConnection([row]), {"vm": "NORTH-Test"})["checkpoints"][0]
    assert listed["notes"] == "one line"


def test_a_checkpoint_without_notes_reports_none() -> None:
    row = _row(FIRST, "s", "20261029014417.778334-000", notes=None)
    listed = checkpoint_list(FakeConnection([row]), {"vm": "NORTH-Test"})["checkpoints"][0]
    assert listed["notes"] is None


def test_restore_applies_the_snapshot_named_by_id() -> None:
    connection = FakeConnection()
    result = checkpoint_restore(connection, {"vm": "NORTH-Test", "snapshot_id": SECOND.lower()})
    assert result["restored"] is True
    assert result["checkpoint"]["id"] == SECOND.lower()
    path, method, parameters = connection.calls[0]
    assert (path, method) == (SERVICE_PATH, "ApplySnapshot")
    assert parameters["Snapshot"].endswith(f'Microsoft:{SECOND}"')


def test_delete_destroys_the_snapshot_named_by_id() -> None:
    connection = FakeConnection()
    result = checkpoint_delete(connection, {"vm": "NORTH-Test", "snapshot_id": FIRST})
    assert result["deleted"] is True
    assert result["checkpoint"]["name"] == "Before-NORTH-live-2026-10-28"
    path, method, parameters = connection.calls[0]
    assert (path, method) == (SERVICE_PATH, "DestroySnapshot")
    assert parameters["AffectedSnapshot"].endswith(f'Microsoft:{FIRST}"')


def test_a_snapshot_job_is_waited_for() -> None:
    connection = FakeConnection(response={"ReturnValue": 4096, "Job": "job"}, jobs=[2, 7])
    checkpoint_delete(connection, {"vm": "NORTH-Test", "snapshot_id": FIRST})
    assert connection.calls[0][1] == "DestroySnapshot"


def test_an_unknown_snapshot_is_not_found() -> None:
    connection = FakeConnection()
    with pytest.raises(WmiFailure) as caught:
        checkpoint_delete(connection, {"vm": "NORTH-Test", "snapshot_id": "00000000-0000-0000-0000-000000000000"})
    assert caught.value.code == "not_found"
    assert str(caught.value) == (
        "Expected checkpoint '00000000-0000-0000-0000-000000000000' on 'NORTH-Test'; found 0."
    )
    assert connection.calls == []


def test_a_host_without_the_snapshot_service_is_unavailable() -> None:
    connection = FakeConnection(has_service=False)
    with pytest.raises(WmiFailure) as caught:
        checkpoint_delete(connection, {"vm": "NORTH-Test", "snapshot_id": FIRST})
    assert caught.value.code == "wmi_unavailable"


def test_the_backend_serves_listing_but_leaves_creating_on_the_bridge() -> None:
    backend = WmiBackend(connect=FakeConnection)
    assert backend.invoke("checkpoint_list", {"vm": "NORTH-Test"})["count"] == 2
    # The provider ignores the name in SnapshotSettings, so creating one here
    # would answer with the wrong name; the worker keeps that operation.
    with pytest.raises(BackendError) as caught:
        backend.invoke("checkpoint_create", {"vm": "NORTH-Test", "name": "x"})
    assert caught.value.code == "wmi_unsupported"
