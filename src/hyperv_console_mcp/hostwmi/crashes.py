"""Hyper-V's own record of a guest crash, read from the host's event log.

This is the one operation in this package that is not WMI: the record lives in
the ``Microsoft-Windows-Hyper-V-Worker-Admin`` channel, and pywin32's event-log
API reads it without a PowerShell round trip and without parsing translated
text.

Event 18590 carries seven named fields — ``VmName``, ``VmId`` and
``VmErrorCode0`` through ``VmErrorCode4`` — which are the bugcheck code and its
four parameters, as numbers. The template was read off this host's provider
(``Get-WinEvent -ListProvider Microsoft-Windows-Hyper-V-Worker``), not assumed:
the bridge reads the same seven fields by position, and reading them by name is
what makes a reordered template a non-event.

The guest's own event 1001 only exists once it has rebooted far enough to log
it, so it cannot classify a crash that is still writing its dump; this can.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree

from .connection import Connection, WmiFailure
from .timefmt import local_iso
from .vm import _int, find_vm

CHANNEL = "Microsoft-Windows-Hyper-V-Worker-Admin"
CRASH_EVENT_ID = 18590
_CODES = ("VmErrorCode0", "VmErrorCode1", "VmErrorCode2", "VmErrorCode3", "VmErrorCode4")
_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"
_MILLISECONDS = 3_600_000
# The channel writes UTC with seven fractional digits, e.g.
# 2026-10-29T19:54:20.6025833Z; datetime.fromisoformat wants three or six.
_SYSTEM_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{1,7})Z")


def host_crash_events(connection: Connection, payload: dict[str, Any]) -> dict[str, Any]:
    vm = find_vm(connection, str(payload.get("vm") or ""))
    # ElementName, not Name: the event carries the friendly name, and Name is
    # the VM's GUID on the CIM side.
    name = str(vm.get("ElementName") or "")
    hours = _int(payload.get("hours")) or 2
    maximum = _int(payload.get("max_events")) or 20
    events = [describe(xml) for xml in _read(hours, maximum)]
    return {
        "vm": name,
        "events": [event for event in events if _is_for(event, name)],
        "log_name": CHANNEL,
    }


def describe(xml: str) -> dict[str, Any]:
    """One event as the bridge reports it, with the codes as ``0x`` strings."""
    root = ElementTree.fromstring(xml)
    system = root.find(f"{_NS}System")
    data = _fields(root)
    codes = [_hex(data.get(field)) for field in _CODES if data.get(field) is not None]
    return {
        "record_id": _number(system.findtext(f"{_NS}EventRecordID")),
        "time": _system_time(system.find(f"{_NS}TimeCreated")),
        "id": _number(system.findtext(f"{_NS}EventID")),
        "vm_name": data.get("VmName") or "",
        "vm_id": data.get("VmId") or "",
        "bugcheck_code": codes[0] if codes else None,
        "parameters": codes[1:5],
    }


def _fields(root: ElementTree.Element) -> dict[str, str]:
    """The named payload, whichever of the two renderings the channel used.

    A manifest template renders as ``<EventData><Data Name="VmName">``, but the
    worker's own events are written as ``<UserData>`` with the field name as the
    element name — 18500, read off this host, is one — and a reader that only
    knows the first shape would answer an empty payload for them.
    """
    named = {
        str(element.get("Name")): element.text
        for element in root.iter(f"{_NS}Data")
        if element.get("Name")
    }
    if named:
        return {name: value or "" for name, value in named.items()}
    user = root.find(f"{_NS}UserData")
    if user is None:
        return {}
    return {
        element.tag.rsplit("}", 1)[-1]: element.text or ""
        for element in user.iter()
        if element is not user
    }


def _read(hours: int, maximum: int) -> list[str]:
    """The matching events, newest first, as their XML text."""
    try:
        import win32evtlog
    except ImportError as exc:
        raise WmiFailure(
            "pywin32 is not installed, so the host's event log cannot be read. "
            "Install the 'wmi' extra or set HYPERV_MCP_HOST_BACKEND=powershell.",
            code="wmi_unavailable",
        ) from exc
    query = (
        f"*[System[(EventID={CRASH_EVENT_ID}) and "
        f"TimeCreated[timediff(@SystemTime) <= {hours * _MILLISECONDS}]]]"
    )
    try:
        handle = win32evtlog.EvtQuery(CHANNEL, win32evtlog.EvtQueryReverseDirection, query)
        raw = win32evtlog.EvtNext(handle, maximum)
    except Exception:
        # The bridge treats a host without the channel — or a caller that cannot
        # read it — as having no crash record, and the guest-side fallback still
        # runs. Matching that keeps the two answers the same on such a host.
        return []
    return [win32evtlog.EvtRender(event, win32evtlog.EvtRenderEventXml) for event in raw]


def _is_for(event: dict[str, Any], name: str) -> bool:
    """The bridge skips a named event for another VM, and keeps an unnamed one."""
    return not event["vm_name"] or event["vm_name"] == name


def _hex(value: str | None) -> str:
    try:
        return f"0x{int(str(value), 0):x}"
    except ValueError:
        return str(value)


def _number(value: str | None) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _system_time(element: Any) -> str | None:
    """The event's UTC stamp, in the local form the bridge prints."""
    match = _SYSTEM_TIME.fullmatch(str(element.get("SystemTime") if element is not None else ""))
    if match is None:
        return None
    fields = [int(match.group(index)) for index in range(1, 7)]
    # Seven digits in, six to datetime and the seventh kept for the output: it
    # is .NET's 100 ns unit, and dropping it would shift the stamp.
    fraction = int(match.group(7).ljust(7, "0"))
    moment = datetime(*fields, fraction // 10, tzinfo=timezone.utc)
    return local_iso(moment.astimezone(), fraction % 10)
