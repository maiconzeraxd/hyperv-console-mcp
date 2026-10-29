"""The shared service over its real pipe: Windows-only, and exercised live here.

Every other test in the suite is platform-neutral; this file starts an actual
named pipe, connects to it, and checks that the envelope, the refusals and the
size limits survive the trip.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

import pytest
from test_service import FakeBackend

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import PolicyError
from hyperv_console_mcp.service import HyperVService
from hyperv_console_mcp.service.host import ServiceClient, ServiceHost, pipe_name
from hyperv_console_mcp.service.remote import ServiceProxy

windows = pytest.mark.skipif(os.name != "nt", reason="named pipes are Windows only")
pytestmark = windows

VM = "NORTH-Test"


def unique_name() -> str:
    """A pipe per test, so a host left running by another test cannot answer."""
    return pipe_name("-" + uuid.uuid4().hex)


def make_service(**overrides: Any) -> HyperVService:
    values: dict[str, Any] = {"allowed_vms": frozenset({VM})}
    values.update(overrides)
    return HyperVService(FakeBackend(), Settings(**values))


@pytest.fixture
def host():
    instance = ServiceHost(make_service(), name=unique_name(), instances=2)
    instance.start()
    try:
        yield instance
    finally:
        instance.close()


def test_a_call_crosses_the_pipe(host: ServiceHost) -> None:
    result = ServiceProxy(ServiceClient(host.name)).list_vms()
    assert result["vms"][0]["name"] == VM


def test_a_refusal_crosses_with_its_code_and_fix(host: ServiceHost) -> None:
    proxy = ServiceProxy(ServiceClient(host.name))
    with pytest.raises(PolicyError) as caught:
        proxy.guest_exec(VM, "hostname")
    assert caught.value.code == "not_authorized"
    assert (caught.value.fix or {}).get("operator", "").startswith("hyperv-access set")


def test_a_private_method_is_refused_across_the_pipe(host: ServiceHost) -> None:
    response = ServiceClient(host.name)({"method": "_write", "args": [VM, "power"], "kwargs": {}})
    assert response["error"]["code"] == "invalid_argument"


def test_a_second_host_refuses_to_share_the_name(host: ServiceHost) -> None:
    second = ServiceHost(make_service(), name=host.name)
    with pytest.raises(PolicyError, match="already owns the pipe"):
        second.start()


def test_a_client_with_no_host_is_told_how_to_start_one() -> None:
    with pytest.raises(PolicyError, match="not running"):
        ServiceClient(unique_name())({"method": "list_vms", "args": [], "kwargs": {}})


def test_available_answers_without_sending_anything(host: ServiceHost) -> None:
    assert ServiceClient(host.name).available() is True
    assert ServiceClient(unique_name()).available() is False


class Big:
    """A result far larger than the pipe buffer, which framing must survive."""

    def blob(self, size: int) -> str:
        return "x" * size


def test_a_result_larger_than_the_pipe_buffer_crosses() -> None:
    instance = ServiceHost(Big(), name=unique_name(), instances=1)
    instance.start()
    try:
        result = ServiceProxy(ServiceClient(instance.name)).blob(2_000_000)
        assert len(result) == 2_000_000
    finally:
        instance.close()


class Counter:
    """Records whether two operations were ever inside the service at once."""

    def __init__(self) -> None:
        self.active = 0
        self.overlaps = 0

    def slow(self) -> dict[str, Any]:
        self.active += 1
        if self.active > 1:
            self.overlaps += 1
        time.sleep(0.05)
        self.active -= 1
        return {"ok": True}


def test_two_callers_never_run_inside_the_service_at_once() -> None:
    """One owner means one operation at a time, however many clients connect."""
    counter = Counter()
    instance = ServiceHost(counter, name=unique_name(), instances=2)
    instance.start()
    results: list[dict[str, Any]] = []

    def call() -> None:
        results.append(ServiceProxy(ServiceClient(instance.name)).slow())

    try:
        threads = [threading.Thread(target=call) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
    finally:
        instance.close()
    assert len(results) == 4
    assert counter.overlaps == 0
