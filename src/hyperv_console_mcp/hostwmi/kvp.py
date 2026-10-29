"""Who the guest is, read from the host without a guest credential.

Hyper-V's key-value pair exchange carries a handful of facts the guest writes
about itself: its computer name, its OS name and build, its integration
services version. Reading them costs one WMI query and no password, which is
what makes them the right source for discovery — a tool can say "this VM is
DESKTOP-AA9PSTJ running Windows 10 Pro" before any credential exists.

Two things the exchange does not answer and this module does not pretend to:

* **Addresses.** ``NetworkAddressIPv4``/``NetworkAddressIPv6`` are deprecated
  and now carry a deprecation notice rather than an address, so they are
  filtered out here. Guest addresses come from
  ``Msvm_GuestNetworkAdapterConfiguration``, which ``vm._adapters`` already
  reads.
* **Anything about a stopped VM.** The component exists but its item list is
  empty, which is reported as ``available: false``, not as a guest with no name.
"""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree

from .connection import Connection, WmiFailure, wql_literal

# The component's DeviceID is the device's own GUID, not the VM's, so the VM is
# identified by SystemName — the scoping computer system's Name, which is the
# same identifier `find_vm` matches on.
_KVP_FIELDS = "DeviceID, SystemName, GuestIntrinsicExchangeItems, GuestExchangeItems"

# Deprecated by the provider: their data is a notice, not a value, and a reader
# that took it for an address would be badly wrong.
_DEPRECATED = frozenset({
    "NetworkAddressIPv4", "NetworkAddressIPv6", "RDPAddressIPv4", "RDPAddressIPv6",
})


def guest_facts(connection: Connection, identifier: str) -> dict[str, Any]:
    """The guest's self-description, or ``available: false`` when it has none.

    A failure is reported inside the answer rather than raised: these facts are
    an enrichment of ``vm_get``, and a VM whose integration service is missing
    should still be listed.
    """
    try:
        rows = connection.query(
            f"SELECT {_KVP_FIELDS} FROM Msvm_KvpExchangeComponent "
            f"WHERE SystemName = {wql_literal(identifier)}"
        )
    except WmiFailure as exc:
        return {"available": False, "error": str(exc)}
    device = rows[0] if rows else None
    if device is None:
        return {"available": False, "error": "The VM has no key-value pair exchange component."}
    intrinsic = _items(device.get("GuestIntrinsicExchangeItems"))
    if not intrinsic:
        return {
            "available": False,
            "error": "The guest has not published its facts; the Data Exchange integration "
                     "service may be stopped.",
        }
    values = {name: data for name, data in intrinsic.items() if name not in _DEPRECATED}
    return {
        "available": True,
        # A guest publishes one or the other: this one has no ComputerName and
        # names itself through FullyQualifiedDomainName.
        "computer_name": values.get("ComputerName") or values.get("FullyQualifiedDomainName"),
        "os_name": values.get("OSName"),
        "os_version": values.get("OSVersion"),
        "os_build": values.get("OSBuildNumber"),
        "integration_services_version": values.get("IntegrationServicesVersion"),
        "values": dict(sorted(values.items())),
        # What the host put there, not what the guest says about itself.
        "host_set": _items(device.get("GuestExchangeItems")),
    }


def _items(fragments: Any) -> dict[str, str]:
    """The name/data pairs of the KVP exchange items, each one an XML fragment.

    A fragment this build cannot parse is skipped rather than failing the whole
    answer: one unfamiliar item should not hide the rest of the guest's facts.
    """
    values: dict[str, str] = {}
    for fragment in fragments or []:
        try:
            root = ElementTree.fromstring(str(fragment))
        except ElementTree.ParseError:
            continue
        name = data = None
        for prop in root.findall("PROPERTY"):
            key = str(prop.get("NAME") or "")
            if key == "Name":
                name = prop.findtext("VALUE")
            elif key == "Data":
                data = prop.findtext("VALUE")
        if name:
            values[name] = data or ""
    return values
