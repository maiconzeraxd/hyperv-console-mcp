"""The boundary the pipe will carry: what crosses, and what is refused.

Nothing here opens a pipe. The transport is a function, so the envelope, the
codec and the dispatch table are exercised in-process on every platform; the
Windows pipe is a byte channel around exactly these functions.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from test_service import FakeBackend

from hyperv_console_mcp.capture import Screenshot
from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import (
    BackendError,
    DebuggerError,
    HyperVError,
    PolicyError,
    ValidationError,
)
from hyperv_console_mcp.service import HyperVService
from hyperv_console_mcp.service.remote import (
    ServiceProxy,
    decode,
    dispatch,
    encode,
    error_from,
    local_transport,
)

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "hyperv_console_mcp"
VM = "NORTH-Test"


@pytest.fixture(autouse=True)
def empty_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hyperv_console_mcp.credentials._read", lambda target: None, raising=False)


def service(**overrides: Any) -> HyperVService:
    values: dict[str, Any] = {
        "allowed_vms": frozenset({VM}),
        "allow_power": True,
        "allow_guest": True,
        "guest_username": "Administrator",
        "guest_password": "secret",
    }
    values.update(overrides)
    return HyperVService(FakeBackend(), Settings(**values))


def proxy(target: HyperVService | None = None) -> ServiceProxy:
    return ServiceProxy(local_transport(target or service()))


def test_a_plain_result_round_trips() -> None:
    assert proxy().list_vms()["vms"][0]["name"] == VM


def test_the_codec_keeps_the_types_json_would_flatten() -> None:
    value = {
        "png": Screenshot(png=b"\x89PNG", metadata={"width": 10}),
        "blob": b"\x00\x01",
        "path": Path("C:/Lab/package"),
        "pair": (1, "two"),
        "nested": [{"deep": (True, None)}],
    }
    restored = decode(encode(value))
    assert restored == value
    assert isinstance(restored["path"], Path)
    assert isinstance(restored["pair"], tuple)
    assert isinstance(restored["png"], Screenshot)


def test_a_value_the_codec_cannot_represent_is_refused() -> None:
    with pytest.raises(TypeError, match="cannot cross"):
        encode(object())


class ScreenshotService:
    """Only what the codec needs to prove a binary result survives the trip."""

    def take(self) -> Screenshot:
        return Screenshot(png=b"\x89PNG-bytes", metadata={"source": "thumbnail"})


def test_a_screenshot_survives_the_boundary() -> None:
    result = ServiceProxy(local_transport(ScreenshotService())).take()
    assert result == Screenshot(png=b"\x89PNG-bytes", metadata={"source": "thumbnail"})


def test_the_event_snapshot_crosses_the_boundary() -> None:
    """The bus belongs to whoever owns the service, so a client asks it.

    A client reading its own bus would always get an empty snapshot once the
    service moved to another process, and the activity page would go quiet.
    """
    from hyperv_console_mcp.events import EVENTS

    snapshot = proxy().event_snapshot()
    assert isinstance(snapshot["events"], list)
    assert snapshot["latest_sequence"] == EVENTS.snapshot()["latest_sequence"]


def test_an_error_keeps_its_code_and_fix() -> None:
    target = service(allow_guest=False, levels={VM.casefold(): "control"})
    with pytest.raises(PolicyError) as caught:
        ServiceProxy(local_transport(target)).guest_exec(VM, "hostname")
    assert caught.value.code == "not_authorized"
    assert caught.value.fix == {"operator": f'hyperv-access set "{VM}" lab'}


@pytest.mark.parametrize(
    "method, kind",
    [
        ("invalid_argument", ValidationError),
        ("not_authorized", PolicyError),
        ("backend_error", BackendError),
        ("kd_timeout", DebuggerError),
    ],
)
def test_a_code_is_rebuilt_as_the_class_that_carries_it(method: str, kind: type) -> None:
    error = error_from({"code": method, "message": "x", "fix": {"operator": "y"}})
    assert isinstance(error, kind)
    assert error.code == method
    assert error.fix == {"operator": "y"}


def test_a_private_method_is_refused_rather_than_looked_up() -> None:
    """``_write`` would run a mutation with the policy check already done."""
    target = service()
    for name in ("_write", "_call", "_approve", "backend", "settings", "close", ""):
        response = dispatch(target, {"method": name})
        assert "error" in response, name
        assert response["error"]["code"] == "invalid_argument"


def test_an_unknown_method_is_refused() -> None:
    response = dispatch(service(), {"method": "format_disk"})
    assert response["error"]["code"] == "invalid_argument"
    assert "format_disk" in response["error"]["message"]


def test_wrong_arguments_are_named_before_the_method_runs() -> None:
    target = service()
    response = dispatch(target, {"method": "power", "args": [VM], "kwargs": {"action": "start", "x": 1}})
    assert response["error"]["code"] == "invalid_argument"
    assert "x" in response["error"]["message"]
    assert target.backend.calls == []


def test_an_unexpected_failure_does_not_leak_its_traceback() -> None:
    target = service()
    target.backend.failure = ZeroDivisionError("1/0 at secret/path.py")
    response = dispatch(target, {"method": "list_vms"})
    assert response["error"] == {
        "code": "internal_error",
        "message": "list_vms failed: ZeroDivisionError",
    }


def test_a_result_that_cannot_cross_is_an_internal_error() -> None:
    class Leaky:
        def thing(self) -> object:
            return object()

    response = dispatch(Leaky(), {"method": "thing"})
    assert response["error"]["code"] == "internal_error"
    assert "cannot cross" in response["error"]["message"]


def test_a_broken_fix_is_not_invented() -> None:
    error = error_from({"code": "backend_error", "message": "x"})
    assert error.fix is None
    assert isinstance(error, HyperVError)


def _called_names(path: Path, pattern: str) -> set[str]:
    return set(re.findall(pattern, path.read_text(encoding="utf-8")))


def test_every_service_method_a_client_calls_exists_on_the_service() -> None:
    """The proxy answers any name; the table only forwards real public methods.

    A name the server or the dashboard calls but the façade does not define
    would become a silent ``invalid_argument`` at run time, so the surface is
    checked here instead.
    """
    # The parenthesis keeps a module path (``service.host``) out of the sweep:
    # only a call is a call.
    server = _called_names(PACKAGE / "server.py", r"service\.([a-z_]+)\(")
    dashboard = _called_names(PACKAGE / "dashboard.py", r"self\.service\.([a-z_]+)\(")
    assert server and dashboard
    # ``settings`` is a local attribute, not a remote method; everything else a
    # client touches must exist on the façade.
    instance = HyperVService(FakeBackend())
    unknown = sorted(
        name for name in server | dashboard
        if name.startswith("_") or not hasattr(instance, name)
    )
    assert unknown == []


class ActorProbe:
    """A service method that reports which actor the bus recorded."""

    def publish(self) -> str:
        from hyperv_console_mcp.events import EVENTS

        return EVENTS.publish("probe", "started", correlation_id="probe").actor


def test_the_envelope_says_who_is_calling(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The actor lives in the service's process, so the client must send it."""
    monkeypatch.setenv("HYPERV_MCP_EVENT_DIRECTORY", str(tmp_path / "events"))
    probe = ActorProbe()
    assert dispatch(probe, {"method": "publish"})["ok"] == "mcp"
    assert dispatch(probe, {"method": "publish", "actor": "dashboard"})["ok"] == "dashboard"
    assert dispatch(probe, {"method": "publish", "actor": "  "})["ok"] == "mcp"


def test_a_dashboard_call_does_not_prompt_itself_for_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window is not asked to approve the click that came from the window."""
    monkeypatch.setenv("HYPERV_MCP_REQUIRE_DASHBOARD_APPROVAL", "true")
    monkeypatch.delenv("HYPERV_MCP_PROFILE", raising=False)
    asked: list[str] = []
    monkeypatch.setattr(
        "hyperv_console_mcp.ipc.request_approval",
        lambda operation, *args, **kwargs: asked.append(operation),
    )

    class Gate:
        def mutate(self) -> str:
            from hyperv_console_mcp.events import require_dashboard_approval

            require_dashboard_approval("vm_power", VM)
            return "ok"

    assert dispatch(Gate(), {"method": "mutate", "actor": "dashboard"})["ok"] == "ok"
    assert asked == []
    assert dispatch(Gate(), {"method": "mutate", "actor": "mcp"})["ok"] == "ok"
    assert asked == ["vm_power"]


class Recorder:
    """Records exactly what arrived, so the envelope can be checked on its own."""

    def __init__(self) -> None:
        self.seen: tuple[Any, ...] | None = None

    def call(self, vm: str, *, timeout_seconds: int = 10, flag: bool = False) -> dict[str, Any]:
        self.seen = (vm, timeout_seconds, flag)
        return {"ok": True}


def test_the_envelope_keeps_keyword_arguments_and_types() -> None:
    """A keyword argument is part of the call, not decoration: it must arrive."""
    recorder = Recorder()
    result = ServiceProxy(local_transport(recorder)).call(VM, timeout_seconds=30, flag=True)
    assert result == {"ok": True}
    assert recorder.seen == (VM, 30, True)
