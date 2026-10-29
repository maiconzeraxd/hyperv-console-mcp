"""Guest facts the host reads without a credential: the key-value pair exchange."""

from __future__ import annotations

from typing import Any

from hyperv_console_mcp.hostwmi.connection import WmiFailure
from hyperv_console_mcp.hostwmi.kvp import guest_facts

VM_ID = "D5AD7D1D-5CB3-441F-A9D7-138640C8F61A"
DEVICE_ID = "Microsoft:2A34B1C2-FD73-4043-8A5B-DD2159BC743F"


class FakeConnection:
    def __init__(self, rows: list[dict[str, Any]] | Exception) -> None:
        self.rows = rows
        self.queries: list[str] = []

    def query(self, wql: str, *, namespace: str = "") -> list[dict[str, Any]]:
        self.queries.append(wql)
        if isinstance(self.rows, Exception):
            raise self.rows
        return self.rows

    def get(self, path: str, *, namespace: str = "") -> dict[str, Any]:
        raise AssertionError("the exchange should not need Get")

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        raise AssertionError("the exchange should not need a method call")


def _item(name: str, data: str) -> str:
    """One exchange item, in the shape the provider returns it."""
    return (
        '<INSTANCE CLASSNAME="Msvm_KvpExchangeDataItem">'
        f'<PROPERTY NAME="Data" TYPE="string"><VALUE>{data}</VALUE></PROPERTY>'
        f'<PROPERTY NAME="Name" TYPE="string"><VALUE>{name}</VALUE></PROPERTY>'
        '<PROPERTY NAME="Source" TYPE="uint16"><VALUE>2</VALUE></PROPERTY>'
        "</INSTANCE>"
    )


def _device(**overrides: Any) -> dict[str, Any]:
    row = {
        "DeviceID": DEVICE_ID,
        "SystemName": VM_ID,
        "GuestIntrinsicExchangeItems": [
            _item("FullyQualifiedDomainName", "DESKTOP-AA9PSTJ"),
            _item("OSName", "Windows 10 Pro"),
            _item("OSVersion", "10.0.19045"),
            _item("OSBuildNumber", "10011"),
            _item("IntegrationServicesVersion", "10.0.19045"),
        ],
        "GuestExchangeItems": [_item("SessionMonitor", "VmbusSessionMonitor")],
    }
    row.update(overrides)
    return row


def test_the_guest_describes_itself_without_a_credential() -> None:
    connection = FakeConnection([_device()])

    facts = guest_facts(connection, VM_ID)

    assert facts["available"] is True
    assert facts["computer_name"] == "DESKTOP-AA9PSTJ"
    assert facts["os_name"] == "Windows 10 Pro"
    assert facts["os_version"] == "10.0.19045"
    assert facts["os_build"] == "10011"
    assert facts["integration_services_version"] == "10.0.19045"
    assert facts["values"]["OSName"] == "Windows 10 Pro"
    # What the host put there, kept apart from what the guest says about itself.
    assert facts["host_set"] == {"SessionMonitor": "VmbusSessionMonitor"}
    # The component is scoped by SystemName: its DeviceID is the device's own GUID.
    assert f"SystemName = '{VM_ID}'" in connection.queries[0]


def test_a_deprecated_address_key_is_not_reported_as_an_address() -> None:
    """NetworkAddressIPv4 now carries a deprecation notice, not an address."""
    device = _device(
        GuestIntrinsicExchangeItems=[
            _item("ComputerName", "DESKTOP-AA9PSTJ"),
            _item("NetworkAddressIPv4", "As seguintes chaves foram preteridas: NetworkAddressIPv4"),
            _item("RDPAddressIPv6", "As seguintes chaves foram preteridas: RDPAddressIPv6"),
        ]
    )

    facts = guest_facts(FakeConnection([device]), VM_ID)

    assert facts["computer_name"] == "DESKTOP-AA9PSTJ"
    assert "NetworkAddressIPv4" not in facts["values"]
    assert "RDPAddressIPv6" not in facts["values"]
    assert not any("preteridas" in value for value in facts["values"].values())


def test_an_item_this_build_cannot_parse_does_not_hide_the_others() -> None:
    device = _device(
        GuestIntrinsicExchangeItems=[
            "<INSTANCE truncated",
            _item("OSName", "Windows 10 Pro"),
        ]
    )

    facts = guest_facts(FakeConnection([device]), VM_ID)

    assert facts["available"] is True
    assert facts["values"] == {"OSName": "Windows 10 Pro"}


def test_a_guest_that_published_nothing_is_not_a_guest_with_no_name() -> None:
    """The Data Exchange integration service stopped, or the VM is not running."""
    facts = guest_facts(FakeConnection([_device(GuestIntrinsicExchangeItems=[])]), VM_ID)

    assert facts["available"] is False
    assert "Data Exchange" in facts["error"]
    assert "computer_name" not in facts


def test_a_vm_with_no_exchange_component_says_so() -> None:
    facts = guest_facts(FakeConnection([]), VM_ID)

    assert facts == {
        "available": False, "error": "The VM has no key-value pair exchange component."
    }


def test_a_failed_query_is_reported_rather_than_raised() -> None:
    """These facts enrich a listing; a broken exchange must not hide the VMs."""
    facts = guest_facts(FakeConnection(WmiFailure("WMI query failed: refused")), VM_ID)

    assert facts["available"] is False
    assert "refused" in facts["error"]
