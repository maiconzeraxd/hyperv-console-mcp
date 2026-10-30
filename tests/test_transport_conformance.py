"""Both guest transports answer the same contract.

`test_transport.py` and `test_ssh_transport.py` each prove one transport against
its own fakes. That leaves the property the lab actually depends on unchecked:
the same call must produce the same result whichever transport serves it. This
file runs one set of assertions over both, so a divergence fails here instead of
surfacing on a guest mid-cycle.

The two fakes below mirror the parts of the guest each transport talks to: the
bridge envelope for PowerShell Direct, and the stub paramiko client (imported
from `test_ssh_transport`) for SSH.
"""

from __future__ import annotations

import hashlib
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_ssh_transport import (
    VM,
    FakeKey,
    FakeRejectPolicy,
    FakeSSHClient,
    FakeSSHException,
    _default_guest,
    provision,
)

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import ValidationError
from hyperv_console_mcp.service import HyperVService
from hyperv_console_mcp.transport import _HEALTH, select_transport
from hyperv_console_mcp.transport import ssh as ssh_module
from hyperv_console_mcp.transport.ssh import vm_key

LIMIT = 64
LONG_OUTPUT = "x" * 5000


def _ssh_exec(command: str) -> dict[str, Any]:
    """What the guest wrapper reports for the three commands these tests send."""
    text = LONG_OUTPUT if command == "long" else ("boom" if command == "fail" else "LAB\r\n")
    failed = command == "fail"
    return {
        "success": not failed,
        "exit_code": 1 if failed else 0,
        "output": text,
        "had_errors": failed,
        "error_records": ["boom"] if failed else [],
    }


def _guest_result(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The file-manager operations, answered the way the guest script answers.

    One body for both transports: the PowerShell Direct fake wraps it in the
    bridge envelope, the SSH fake hands it to the stub client's guest handler.
    If the two transports ever disagree about these shapes, it is the service
    callers below that notice, which is the point of this file.
    """
    path = str(payload["path"])
    if operation == "guest_list_dir":
        return {"directory": {"path": path, "entries": [
            {"name": "Packages", "path": path + "\\Packages", "type": "directory",
             "size": None, "last_write_utc": "2026-10-29T00:00:00+00:00"},
            {"name": "run.log", "path": path + "\\run.log", "type": "file",
             "size": 12, "last_write_utc": "2026-10-29T00:00:00+00:00"},
        ]}}
    if operation == "guest_make_dir":
        return {"directory": {"path": path, "created": True}}
    if operation == "guest_rename":
        parent = path.rpartition("\\")[0]
        return {"entry": {
            "path": parent + "\\" + str(payload["new_name"]), "previous_path": path,
        }}
    if operation == "guest_delete":
        return {"deleted": {
            "path": path, "type": "directory",
            "recursive": bool(payload.get("recursive")),
        }}
    raise AssertionError(f"the guest was asked for an operation it does not serve: {operation}")


class ConformingBridge:
    """The bridge envelope, with the guest-side behaviour the real one has."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke(
        self, operation: str, payload: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((operation, dict(payload)))
        vm = payload.get("vm")
        if operation in {"guest_list_dir", "guest_make_dir", "guest_rename", "guest_delete"}:
            return {"vm": vm, **_guest_result(operation, payload)}
        if operation == "guest_exec":
            limit = int(payload.get("max_output_chars") or LIMIT)
            report = _ssh_exec(payload["command"])
            cut = len(report["output"]) > limit
            report["output"] = report["output"][:limit] if cut else report["output"]
            report["output_truncated"] = cut
            return {"vm": vm, "execution": report}
        if operation == "guest_status":
            return {"vm": vm, "guest": {"computer_name": "LAB"}}
        if operation == "guest_put_file":
            content = Path(payload["local_path"]).read_bytes()
            self.files[payload["guest_path"]] = content
            return {"vm": vm, "file": {
                "path": payload["guest_path"], "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }}
        if operation == "guest_get_file":
            content = self.files[payload["guest_path"]]
            target = Path(payload["local_path"])
            target.write_bytes(content)
            return {"vm": vm, "file": {
                "path": str(target), "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }}
        raise AssertionError(f"the bridge was asked for an operation it does not serve: {operation}")


@dataclass
class Case:
    name: str
    service: HyperVService
    transport: Any
    settings: Settings
    bridge: ConformingBridge


@pytest.fixture(autouse=True)
def fake_world(monkeypatch: pytest.MonkeyPatch):
    """No real vault, no real paramiko, no cached client between cases."""
    monkeypatch.setattr(
        "hyperv_console_mcp.credentials._read", lambda target: None, raising=False
    )
    module = types.ModuleType("paramiko")
    module.SSHClient = FakeSSHClient
    module.RejectPolicy = FakeRejectPolicy
    module.Ed25519Key = FakeKey
    module.SSHException = FakeSSHException
    monkeypatch.setitem(sys.modules, "paramiko", module)
    FakeSSHClient.instances.clear()
    # The stub keeps its behaviour on the class, so a test in the other module
    # can leave it in a mode that would silently change these results.
    for attribute, value in {
        "reject_host_key": False, "health_fails": False, "corrupt_upload": False,
        "corrupt_download": False, "raw_output": None, "reject_file_run": False,
        "refuse_exec": False, "refuse_connect": False, "preload_files": {},
    }.items():
        setattr(FakeSSHClient, attribute, value)
    FakeSSHClient.exec_handler = _ssh_exec
    FakeSSHClient.guest_handler = _guest_result
    ssh_module._CLIENTS.clear()
    _HEALTH.clear()
    yield
    FakeSSHClient.guest_handler = _default_guest
    ssh_module._CLIENTS.clear()
    _HEALTH.clear()


@pytest.fixture(params=["psdirect", "ssh"])
def case(request: pytest.FixtureRequest, tmp_path: Path) -> Case:
    bridge = ConformingBridge()
    (tmp_path / "transfers").mkdir()
    values: dict[str, Any] = {
        "allowed_vms": frozenset({VM}),
        "allow_guest": True,
        "guest_username": "mcn1",
        "guest_password": "secret",
        "guest_transport": request.param,
        "transfer_root": tmp_path / "transfers",
        "ssh_root": tmp_path / "ssh",
        "max_output_chars": LIMIT,
    }
    if request.param == "ssh":
        provision(tmp_path)
    settings = Settings(**values)
    return Case(
        name=request.param,
        service=HyperVService(bridge, settings),
        transport=select_transport(settings, bridge, VM),
        settings=settings,
        bridge=bridge,
    )


def test_the_transport_that_serves_the_call_is_named_in_every_result(case: Case) -> None:
    executed = case.service.guest_exec(VM, "hostname")
    assert executed["execution"]["transport"] == case.name
    status = case.service.guest_status(VM)
    assert status["transport"] == case.name
    assert status["guest"]["transport"] == case.name
    assert status["guest"]["computer_name"] == "LAB"


def test_exec_reports_the_same_keys_from_both_transports(case: Case) -> None:
    execution = case.service.guest_exec(VM, "hostname")["execution"]
    assert set(execution) == {
        "success", "exit_code", "output", "had_errors", "error_records",
        "output_truncated", "transport",
    }
    assert execution["success"] is True
    assert execution["exit_code"] == 0
    assert execution["output"] == "LAB\r\n"


def test_exec_truncates_at_the_configured_limit_on_both_transports(case: Case) -> None:
    execution = case.service.guest_exec(VM, "long")["execution"]
    assert len(execution["output"]) == LIMIT
    assert execution["output_truncated"] is True


def test_a_failing_command_is_a_failed_result_and_not_an_exception(case: Case) -> None:
    execution = case.service.guest_exec(VM, "fail")["execution"]
    assert execution["success"] is False
    assert execution["exit_code"] == 1
    assert execution["had_errors"] is True
    assert execution["error_records"] == ["boom"]


def test_put_then_get_round_trips_the_same_bytes(case: Case, tmp_path: Path) -> None:
    source = tmp_path / "transfers" / "driver.sys"
    payload = b"MZ" + bytes(range(256))
    source.write_bytes(payload)

    written = case.service.guest_put_file(VM, str(source), r"C:\DriverLab\driver.sys")
    assert written["file"]["transport"] == case.name
    assert written["file"]["size"] == len(payload)
    assert written["file"]["sha256"] == hashlib.sha256(payload).hexdigest()

    target = tmp_path / "transfers" / "collected" / "driver.sys"
    fetched = case.service.guest_get_file(VM, r"C:\DriverLab\driver.sys", str(target))
    assert target.read_bytes() == payload
    assert fetched["file"]["sha256"] == written["file"]["sha256"]
    assert fetched["file"]["transport"] == case.name


def test_the_file_manager_answers_the_same_shape_from_both_transports(case: Case) -> None:
    """Listing, mkdir and rename: one set of keys whichever transport served it."""
    listed = case.service.guest_list_dir(VM, r"C:\DriverLab")
    assert listed["transport"] == case.name
    assert listed["vm"] == VM
    assert listed["directory"]["path"] == r"C:\DriverLab"
    assert [entry["name"] for entry in listed["directory"]["entries"]] == [
        "Packages", "run.log",
    ]

    made = case.service.guest_make_dir(VM, r"C:\DriverLab\new")
    assert made["directory"] == {"path": r"C:\DriverLab\new", "created": True}
    assert made["transport"] == case.name

    renamed = case.service.guest_rename(VM, r"C:\DriverLab\new", "renamed")
    assert renamed["entry"] == {
        "path": r"C:\DriverLab\renamed", "previous_path": r"C:\DriverLab\new",
    }
    assert renamed["transport"] == case.name


def test_a_delete_carries_the_recursive_flag_to_both_transports(case: Case) -> None:
    deleted = case.service.guest_delete(
        VM, r"C:\DriverLab\old", r"C:\DriverLab\old", recursive=True
    )
    assert deleted["deleted"] == {
        "path": r"C:\DriverLab\old", "type": "directory", "recursive": True,
    }
    assert deleted["transport"] == case.name


def test_a_structured_operation_outside_the_shared_set_is_refused_first(case: Case) -> None:
    with pytest.raises(ValidationError, match="is not a guest operation"):
        case.transport.call(VM, "guest_not_an_operation", {})
    assert case.bridge.calls == []


def test_the_ssh_transport_is_not_selected_by_accident(case: Case) -> None:
    """A `psdirect` case must not be quietly served by the SSH client."""
    if case.name == "psdirect":
        assert case.transport.name == "psdirect"
        assert not FakeSSHClient.instances
    else:
        assert case.transport.name == "ssh"
        assert (case.settings.ssh_root / vm_key(VM) / "state.json").exists()
