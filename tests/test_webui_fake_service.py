"""The page's whole contract, answered by a fake.

The window has no Hyper-V, no guest and no WebView in a test run, and it should
not need them to be known-correct: what the page asks for is a list of names,
and a fake service that answers every one of them proves the list is answerable
and that the bridge turns each answer into the envelope the page reads. What
this cannot prove is that the real service returns the shapes the page draws —
that is what the service's own tests are for.
"""

from __future__ import annotations

from typing import Any

from test_webui_contract import service_calls

from hyperv_console_mcp.webui.api import Api


class FakeService:
    """Answers any public name with the name and the arguments it was given."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def answer(**kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, dict(kwargs)))
            return {"method": name, "kwargs": kwargs}

        answer.__name__ = name
        return answer


class StrictService:
    """One method with a required argument, to check a binding failure is an answer."""

    def get_vm(self, vm_name: str) -> dict[str, Any]:
        return {"vm": vm_name}


def test_every_name_the_page_calls_is_answered_by_a_fake_service() -> None:
    service = FakeService()
    api = Api(service, settings=None)
    answered = {}
    for name in sorted(service_calls()):
        response = api.call(name, {})
        assert "error" not in response, (name, response)
        answered[name] = response["ok"]["method"]
    assert answered == {name: name for name in sorted(service_calls())}
    # The fake saw one call per name, which is the whole contract exercised.
    assert sorted(name for name, _ in service.calls) == sorted(service_calls())


def test_an_unknown_name_is_a_refusal_and_not_an_exception() -> None:
    """The page draws refusals; a traceback would be a blank window."""
    api = Api(StrictService(), settings=None)
    response = api.call("power", {"vm_name": "A", "action": "start"})
    assert response["error"]["code"] == "invalid_argument"
    assert "power" in response["error"]["message"]


def test_the_wrong_arguments_are_a_refusal_that_names_the_method() -> None:
    api = Api(StrictService(), settings=None)
    response = api.call("get_vm", {})
    assert response["error"]["code"] == "invalid_argument"
    assert "get_vm" in response["error"]["message"]


def test_the_bridge_passes_the_arguments_it_was_given() -> None:
    service = FakeService()
    api = Api(service, settings=None)
    api.call("get_vm", {"vm_name": "LAB-Test"})
    assert service.calls == [("get_vm", {"vm_name": "LAB-Test"})]


def test_a_private_name_never_crosses_the_bridge() -> None:
    """``_call`` and ``_approve`` would run an operation past its own gates."""
    service = FakeService()
    api = Api(service, settings=None)
    response = api.call("_approve", {"operation": "power"})
    assert response["error"]["code"] == "invalid_argument"
    assert service.calls == []
