"""The COM boundary itself: how a method is called and how an object is flattened.

These are the two places where pywin32's behaviour, not the provider's, decides
the answer — a parameterless method and a property write — so they are pinned
with a fake object graph instead of a hypervisor.
"""

from __future__ import annotations

from typing import Any

import pytest

from hyperv_console_mcp.hostwmi.connection import ComConnection, WmiFailure

NAMESPACE = "root\\virtualization\\v2"


class Property:
    def __init__(self, name: str, value: Any = None) -> None:
        self.Name = name
        self.Value = value


class ObjectPath:
    def __init__(self, path: str, cls: str) -> None:
        self.Path = path
        self.Class = cls


class Result:
    """What ExecMethod answers: a property collection and a path."""

    def __init__(self, values: dict[str, Any], path: str = "", cls: str = "") -> None:
        self.Properties_ = [Property(name, value) for name, value in values.items()]
        self.Path_ = ObjectPath(path, cls)


class Properties:
    def __init__(self) -> None:
        self.items: dict[str, Property] = {}

    def Item(self, name: str) -> Property:  # noqa: N802 - pywin32's spelling
        return self.items.setdefault(name, Property(name))


class InParameters:
    """A method's parameter instance; ``setattr`` on it raises, as pywin32 does."""

    def __init__(self) -> None:
        self.Properties_ = Properties()

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "Properties_":
            object.__setattr__(self, name, value)
            return
        raise KeyError(name)

    def SpawnInstance_(self) -> InParameters:  # noqa: N802 - pywin32's spelling
        return self


class Method:
    def __init__(self, in_parameters: Any) -> None:
        self.InParameters = in_parameters


class Target:
    def __init__(self, methods: dict[str, Any]) -> None:
        self.methods = methods

    def Methods_(self, name: str) -> Method:  # noqa: N802 - pywin32's spelling
        return Method(self.methods[name])


class Service:
    def __init__(self, target: Target, result: Result | None = None) -> None:
        self.target = target
        self.result = result or Result({"ReturnValue": 0})
        self.calls: list[tuple[str, str, Any]] = []

    def Get(self, path: str) -> Target:  # noqa: N802 - pywin32's spelling
        return self.target

    def ExecMethod(self, path: str, method: str, in_parameters: Any) -> Result:  # noqa: N802
        self.calls.append((path, method, in_parameters))
        return self.result


def _connection(service: Service) -> ComConnection:
    """A ComConnection with the COM locator replaced by the fake service."""
    connection = object.__new__(ComConnection)
    connection._computer = "."
    connection._locator = None
    connection._services = {NAMESPACE: service}
    return connection


def test_a_method_with_no_parameters_is_called_with_none() -> None:
    """InjectNonMaskableInterrupt answers None for InParameters; ExecMethod takes it."""
    service = Service(Target({"InjectNonMaskableInterrupt": None}))
    assert _connection(service).call("VM", "InjectNonMaskableInterrupt") == {"ReturnValue": 0}
    assert service.calls == [("VM", "InjectNonMaskableInterrupt", None)]


def test_a_parameter_sent_to_a_method_without_them_is_refused() -> None:
    service = Service(Target({"InjectNonMaskableInterrupt": None}))
    with pytest.raises(WmiFailure) as caught:
        _connection(service).call("VM", "InjectNonMaskableInterrupt", KeyCode=13)
    assert caught.value.code == "invalid_argument"
    assert service.calls == []


def test_parameters_are_written_through_the_property_set() -> None:
    """``setattr`` raises on a spawned instance, so the write has to go via Item."""
    service = Service(Target({"PressKey": InParameters()}))
    _connection(service).call("VM", "PressKey", KeyCode=13)
    in_parameters = service.calls[0][2]
    assert in_parameters.Properties_.Item("KeyCode").Value == 13


def test_the_result_keeps_its_path_and_class() -> None:
    """An association answers with several classes, and the mapper tells them apart."""
    result = Result({"ReturnValue": 0}, path="\\\\HOST\\root\\v2:Msvm_Keyboard", cls="Msvm_Keyboard")
    answer = _connection(Service(Target({"PressKey": InParameters()}), result)).call(
        "VM", "PressKey", KeyCode=13
    )
    assert answer["__PATH"] == "\\\\HOST\\root\\v2:Msvm_Keyboard"
    assert answer["__CLASS"] == "Msvm_Keyboard"
