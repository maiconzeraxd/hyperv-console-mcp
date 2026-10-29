"""The host's own record of a crash: event 18590, read by field name.

The reader is pywin32's event-log API, which only exists on Windows. The tests
put a fake module in ``sys.modules`` instead of skipping, because the part worth
testing is the mapping — seven named fields onto the shape the bridge answers —
and that is the same on every platform.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from typing import Any

import pytest

from hyperv_console_mcp.hostwmi import WmiBackend, crashes
from hyperv_console_mcp.hostwmi.connection import WmiFailure
from hyperv_console_mcp.hostwmi.crashes import describe, host_crash_events

VM_ID = "D5AD7D1D-5CB3-441F-A9D7-138640C8F61A"
STAMP = "2026-10-29T19:54:20.6025833Z"
CODES = ("0x9c", "0x0", "0xfffff80512345678", "0x2", "0x0")

# Event 18500, copied verbatim from NORTH-Test's channel. The worker's own
# events render as <UserData> with the field name as the element name, not as
# the <Data Name="..."> an EventData template would give.
REAL_USER_DATA = (
    "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'>"
    "<System><Provider Name='Microsoft-Windows-Hyper-V-Worker' "
    "Guid='{51ddfa29-d5c8-4803-be4b-2ecb715570fe}'/><EventID>18500</EventID>"
    "<Version>0</Version><Level>4</Level><Task>0</Task><Opcode>0</Opcode>"
    "<Keywords>0x8000000000000000</Keywords>"
    "<TimeCreated SystemTime='2026-10-29T19:54:20.0661818Z'/>"
    "<EventRecordID>217</EventRecordID>"
    "<Correlation ActivityID='{cfc2a2da-6698-0001-b80e-e0cf9866dd01}'/>"
    "<Execution ProcessID='10536' ThreadID='3416'/>"
    "<Channel>Microsoft-Windows-Hyper-V-Worker-Admin</Channel>"
    "<Computer>DESKTOP-B3F2A0A</Computer>"
    "<Security UserID='S-1-5-83-1-3584916765-1142906035-2249447337-452380736'/>"
    "</System><UserData>"
    "<VmlEventLog xmlns='http://www.microsoft.com/Windows/Virtualization/Events'>"
    f"<VmName>NORTH-Test</VmName><VmId>{VM_ID}</VmId>"
    "</VmlEventLog></UserData></Event>"
)


_CODE_NAMES = ("VmErrorCode0", "VmErrorCode1", "VmErrorCode2", "VmErrorCode3", "VmErrorCode4")


def user_data(codes: tuple[str, ...] = CODES, record: int = 900) -> str:
    """A crash rendered the way the worker writes its own events."""
    fields = "".join(
        f"<{name}>{value}</{name}>" for name, value in zip(_CODE_NAMES, codes, strict=True)
    )
    return (
        "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'>"
        f"<System><EventID>18590</EventID><TimeCreated SystemTime='{STAMP}'/>"
        f"<EventRecordID>{record}</EventRecordID></System><UserData>"
        "<VmlEventLog xmlns='http://www.microsoft.com/Windows/Virtualization/Events'>"
        f"<VmName>NORTH-Test</VmName><VmId>{VM_ID}</VmId>{fields}"
        "</VmlEventLog></UserData></Event>"
    )


def event(
    *,
    name: str = "NORTH-Test",
    identifier: str = VM_ID,
    codes: tuple[str, ...] = ("0x9c", "0x0", "0xfffff80512345678", "0x2", "0x0"),
    record: int = 4242,
    stamp: str = STAMP,
    order: tuple[str, ...] = ("VmName", "VmId", *_CODE_NAMES),
) -> str:
    """One rendered event, as ``EvtRender`` hands it over."""
    values = {"VmName": name, "VmId": identifier}
    values.update(dict(zip(_CODE_NAMES, codes, strict=True)))
    data = "".join(f'<Data Name="{field}">{values[field]}</Data>' for field in order)
    return (
        '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">'
        f'<System><Provider Name="Microsoft-Windows-Hyper-V-Worker"/>'
        f"<EventID>18590</EventID><Version>0</Version>"
        f'<TimeCreated SystemTime="{stamp}"/>'
        f"<EventRecordID>{record}</EventRecordID>"
        "<Channel>Microsoft-Windows-Hyper-V-Worker-Admin</Channel>"
        "<Computer>DESKTOP-B3F2A0A</Computer></System>"
        f"<EventData>{data}</EventData></Event>"
    )


class FakeEventLog:
    """The slice of ``win32evtlog`` the reader uses."""

    EvtQueryReverseDirection = 1
    EvtRenderEventXml = 1

    def __init__(self, *xmls: str, failing: bool = False) -> None:
        self.xmls = list(xmls)
        self.failing = failing
        self.queries: list[tuple[str, int, str]] = []
        self.limits: list[int] = []

    def EvtQuery(self, channel: str, flags: int, query: str) -> str:
        self.queries.append((channel, flags, query))
        if self.failing:
            raise OSError("No events were found that match the specified selection criteria.")
        return "handle"

    def EvtNext(self, handle: str, maximum: int) -> list[str]:
        self.limits.append(maximum)
        return list(self.xmls)

    def EvtRender(self, event: str, flags: int) -> str:
        return event


class FakeConnection:
    def __init__(self, name: str = "NORTH-Test") -> None:
        self.name = name

    def query(self, wql: str, *, namespace: str = "") -> list[dict[str, Any]]:
        if "FROM Msvm_ComputerSystem" in wql:
            return [
                {
                    "ElementName": self.name,
                    "Name": VM_ID,
                    "EnabledState": 2,
                    "StatusDescriptions": [],
                    "OnTimeInMilliseconds": 1000,
                    "__PATH": f'\\\\HOST\\root\\virtualization\\v2:Msvm_ComputerSystem.Name="{VM_ID}"',
                }
            ]
        return [{"__PATH": "x"}]  # the permission guard's service query

    def get(self, path: str, *, namespace: str = "") -> dict[str, Any]:
        raise AssertionError("a crash event is not a CIM object")

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        raise AssertionError("a crash event is not a method call")


def install(monkeypatch: pytest.MonkeyPatch, log: FakeEventLog) -> FakeEventLog:
    monkeypatch.setitem(sys.modules, "win32evtlog", log)
    return log


def test_the_seven_fields_become_the_bridge_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEventLog(event()))
    result = host_crash_events(FakeConnection(), {"vm": "NORTH-Test"})
    assert result["vm"] == "NORTH-Test"
    assert result["log_name"] == "Microsoft-Windows-Hyper-V-Worker-Admin"
    (crash,) = result["events"]
    assert crash["record_id"] == 4242
    assert crash["id"] == 18590
    assert crash["vm_name"] == "NORTH-Test"
    assert crash["vm_id"] == VM_ID
    assert crash["bugcheck_code"] == "0x9c"
    assert crash["parameters"] == list(CODES[1:5])


def test_the_vm_is_matched_by_its_friendly_name_not_its_guid() -> None:
    """``Name`` is the GUID on the CIM side and the friendly name in the event."""
    crash = describe(event(name="NORTH-Test"))
    assert crash["vm_name"] == "NORTH-Test"
    assert crash["vm_id"] == VM_ID


def test_the_fields_are_read_by_name_so_a_reordered_template_still_maps() -> None:
    reordered = ("VmErrorCode2", "VmId", "VmName", "VmErrorCode0",
                 "VmErrorCode1", "VmErrorCode3", "VmErrorCode4")
    crash = describe(event(order=reordered))
    assert crash["vm_name"] == "NORTH-Test"
    assert crash["bugcheck_code"] == "0x9c"
    assert crash["parameters"] == ["0x0", "0xfffff80512345678", "0x2", "0x0"]


def test_a_user_data_event_is_read_by_element_name() -> None:
    """The worker writes <UserData> with the field name as the element name."""
    crash = describe(user_data())
    assert crash["record_id"] == 900
    assert crash["vm_name"] == "NORTH-Test"
    assert crash["vm_id"] == VM_ID
    assert crash["bugcheck_code"] == "0x9c"
    assert crash["parameters"] == list(CODES[1:5])


def test_a_real_user_data_event_from_the_host_maps_without_codes() -> None:
    """18500 is what NORTH-Test's channel actually holds; it has no bugcheck."""
    crash = describe(REAL_USER_DATA)
    assert crash["record_id"] == 217
    assert crash["id"] == 18500
    assert crash["vm_name"] == "NORTH-Test"
    assert crash["vm_id"] == VM_ID
    assert crash["bugcheck_code"] is None
    assert crash["parameters"] == []
    assert crash["time"] is not None


def test_an_event_with_neither_rendering_has_no_fields() -> None:
    crash = describe("<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'>"
                     "<System><EventID>18590</EventID></System></Event>")
    assert crash["vm_name"] == ""
    assert crash["bugcheck_code"] is None


def test_the_codes_come_out_as_hex_strings() -> None:
    crash = describe(event(codes=("1572", "0", "18446744073709551615", "10", "0")))
    assert crash["bugcheck_code"] == "0x624"
    assert crash["parameters"][0] == "0x0"
    assert crash["parameters"][1] == "0xffffffffffffffff"
    assert crash["parameters"][2] == "0xa"


def test_a_code_that_is_not_a_number_is_kept_as_text() -> None:
    crash = describe(event(codes=("n/a", "0", "0", "0", "0")))
    assert crash["bugcheck_code"] == "n/a"


def test_the_stamp_is_the_local_instant_the_bridge_prints() -> None:
    crash = describe(event())
    assert crash["time"] is not None
    moment = datetime.fromisoformat(str(crash["time"]))
    assert moment == datetime(2026, 10, 29, 19, 54, 20, 602583, tzinfo=timezone.utc)
    # Seven fractional digits and an offset, as ToString('o') writes it, and the
    # seventh digit is .NET's 100 ns unit rather than padding.
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.6025833[+-]\d{2}:\d{2}", str(crash["time"])
    )


def test_a_stamp_that_does_not_parse_becomes_null() -> None:
    assert describe(event(stamp=""))["time"] is None
    assert describe(event(stamp="2026-10-29 19:54:20"))["time"] is None


def test_events_for_another_vm_are_skipped_and_unnamed_ones_are_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bridge keeps an event with no name and drops one named for another VM."""
    install(
        monkeypatch,
        FakeEventLog(event(record=1), event(name="", record=2), event(name="OTHER", record=3)),
    )
    result = host_crash_events(FakeConnection(), {"vm": "NORTH-Test"})
    assert [crash["record_id"] for crash in result["events"]] == [1, 2]


def test_the_window_and_the_limit_reach_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    log = install(monkeypatch, FakeEventLog())
    host_crash_events(FakeConnection(), {"vm": "NORTH-Test", "hours": 6, "max_events": 5})
    (channel, flags, query), = log.queries
    assert channel == "Microsoft-Windows-Hyper-V-Worker-Admin"
    assert flags == FakeEventLog.EvtQueryReverseDirection
    assert "EventID=18590" in query
    assert "timediff(@SystemTime) <= 21600000" in query
    assert log.limits == [5]


def test_the_defaults_are_the_bridge_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    log = install(monkeypatch, FakeEventLog())
    host_crash_events(FakeConnection(), {"vm": "NORTH-Test"})
    assert "timediff(@SystemTime) <= 7200000" in log.queries[0][2]
    assert log.limits == [20]


def test_a_host_without_the_channel_answers_no_events(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEventLog(failing=True))
    assert host_crash_events(FakeConnection(), {"vm": "NORTH-Test"})["events"] == []


def test_without_pywin32_the_operation_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "win32evtlog", None)
    with pytest.raises(WmiFailure) as caught:
        host_crash_events(FakeConnection(), {"vm": "NORTH-Test"})
    assert caught.value.code == "wmi_unavailable"


def test_an_unknown_vm_is_refused_before_the_log_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = install(monkeypatch, FakeEventLog())
    with pytest.raises(WmiFailure) as caught:
        host_crash_events(FakeConnection(), {"vm": "NOPE"})
    assert caught.value.code == "not_found"
    assert log.queries == []


def test_the_backend_serves_host_crash_events(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEventLog(event()))
    backend = WmiBackend(connect=FakeConnection)
    assert backend.invoke("host_crash_events", {"vm": "NORTH-Test"})["events"]


def test_the_channel_and_the_id_are_the_documented_ones() -> None:
    assert crashes.CHANNEL == "Microsoft-Windows-Hyper-V-Worker-Admin"
    assert crashes.CRASH_EVENT_ID == 18590
