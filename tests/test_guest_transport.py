"""Transport health: which path a VM is on, whether it answers, and how fast.

The green light in the window is not "SSH was configured once" — it is "this VM
answered a command just now, in this many milliseconds". These cover the two
halves of that: the probe that measures it (and turns a failure into an answer
instead of an exception) and the selection that decides which path was measured.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import HyperVError, PolicyError, ValidationError
from hyperv_console_mcp.service import HyperVService

VM = "NORTH-Test"


@pytest.fixture(autouse=True)
def empty_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite must not depend on what this machine's Credential Manager holds."""
    monkeypatch.setattr(
        "hyperv_console_mcp.credentials._read", lambda target: None, raising=False
    )


class FakeBackend:
    """A guest that answers `guest_exec` with its own name, or refuses."""

    def __init__(self, *, answering: bool = True) -> None:
        self.calls: list[str] = []
        self.answering = answering

    def invoke(self, operation: str, payload: dict[str, Any], *, timeout: Any = None):
        self.calls.append(operation)
        if operation == "vm_get":
            return {"vm": {"name": payload["vm"], "state": "Running"}}
        if operation == "guest_exec":
            if not self.answering:
                raise HyperVError("the guest did not answer", code="guest_unreachable")
            return {"vm": payload["vm"], "execution": {
                "success": True, "exit_code": 0, "output": "NORTH-Test\r\n",
                "had_errors": False, "error_records": [], "output_truncated": False,
            }}
        raise AssertionError(f"unexpected operation {operation}")


def service(
    tmp_path: Path, backend: FakeBackend | None = None, **settings: Any
) -> HyperVService:
    values = {
        "allowed_vms": frozenset({VM}),
        "allow_guest": True,
        "guest_username": "lab",
        "guest_password": "secret",
        "ssh_root": tmp_path / "ssh",
        "transport_choice_file": tmp_path / "transport.json",
        **settings,
    }
    return HyperVService(backend or FakeBackend(), Settings(**values))


def provision(tmp_path: Path) -> None:
    """Write the state `guest_ssh_provision` leaves behind, without an SSH server."""
    directory = tmp_path / "ssh" / VM.casefold()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "state.json").write_text(
        json.dumps({"host": "192.168.50.10", "port": 22}), encoding="utf-8"
    )


# -- the probe -----------------------------------------------------------

def test_the_probe_measures_the_round_trip_and_names_the_guest(tmp_path: Path) -> None:
    host = service(tmp_path)

    result = host.guest_transport_status(VM)

    assert result["vm"] == VM
    assert result["selected"] == "psdirect"
    assert result["healthy"] is True
    assert isinstance(result["latency_ms"], int)
    assert result["latency_ms"] >= 0
    assert result["guest"] == VM
    assert result["state"] == "Running"
    assert result["error"] is None


def test_a_guest_that_does_not_answer_is_red_not_an_exception(tmp_path: Path) -> None:
    """The indicator has to draw a red light; it cannot be an error to the page."""
    host = service(tmp_path, FakeBackend(answering=False))

    result = host.guest_transport_status(VM)

    assert result["healthy"] is False
    assert result["error"] == "guest_unreachable"
    assert result["selected"] == "psdirect"
    assert result["latency_ms"] >= 0


def test_without_a_probe_nothing_reaches_the_guest(tmp_path: Path) -> None:
    """`probe=False` is the cheap read the page polls; it must not run a command."""
    backend = FakeBackend()
    host = service(tmp_path, backend)

    result = host.guest_transport_status(VM, probe=False)

    assert result["healthy"] is None
    assert result["latency_ms"] is None
    assert result["selected"] == "psdirect"
    assert result["configured"] == "auto"
    assert "guest_exec" not in backend.calls


def test_the_probe_flag_must_be_a_boolean(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="boolean"):
        service(tmp_path).guest_transport_status(VM, probe="yes")  # type: ignore[arg-type]


def test_the_answer_names_every_transport_the_host_offers(tmp_path: Path) -> None:
    result = service(tmp_path).guest_transport_status(VM, probe=False)
    assert result["transports"] == ["auto", "psdirect", "ssh"]


# -- the selection -------------------------------------------------------

def test_an_explicit_ssh_on_an_unprovisioned_vm_is_reported_not_raised(
    tmp_path: Path,
) -> None:
    """Asking for SSH on a VM that never got it is this VM's state, not a failure."""
    host = service(tmp_path, guest_transport="ssh")

    result = host.guest_transport_status(VM)

    assert result["configured"] == "ssh"
    assert result["selected"] is None
    assert result["error"] == "not_authorized"
    assert "provision" in result["message"]
    assert result["ssh"]["provisioned"] is False


def test_a_provisioned_ssh_vm_reports_both_paths(tmp_path: Path) -> None:
    """SSH is measured on its own, so the page can show it even when PS Direct carries the call."""
    provision(tmp_path)
    host = service(tmp_path, guest_transport="psdirect")
    health = {"computer_name": VM, "windows_version": "10.0.19045", "transport": "ssh"}
    monkeypatched = []

    class FakeSsh:
        def __init__(self, settings: Any) -> None:
            pass

        def is_provisioned(self, vm: str) -> bool:
            return True

        def health(self, vm: str) -> dict[str, Any]:
            monkeypatched.append(vm)
            return health

    import hyperv_console_mcp.transport.ssh as ssh_module

    original = ssh_module.SshTransport
    ssh_module.SshTransport = FakeSsh  # type: ignore[assignment]
    try:
        result = host.guest_transport_status(VM)
    finally:
        ssh_module.SshTransport = original  # type: ignore[assignment]

    assert result["selected"] == "psdirect"
    assert monkeypatched == [VM]
    assert result["ssh"]["provisioned"] is True
    assert result["ssh"]["healthy"] is True
    assert result["ssh"]["guest"]["computer_name"] == VM
    assert result["ssh"]["latency_ms"] >= 0


def test_an_ssh_probe_that_fails_says_why(tmp_path: Path) -> None:
    provision(tmp_path)
    host = service(tmp_path, guest_transport="psdirect")

    class FailingSsh:
        def __init__(self, settings: Any) -> None:
            pass

        def is_provisioned(self, vm: str) -> bool:
            return True

        def health(self, vm: str) -> dict[str, Any]:
            raise HyperVError("connection refused", code="ssh_unavailable")

    import hyperv_console_mcp.transport.ssh as ssh_module

    original = ssh_module.SshTransport
    ssh_module.SshTransport = FailingSsh  # type: ignore[assignment]
    try:
        result = host.guest_transport_status(VM)
    finally:
        ssh_module.SshTransport = original  # type: ignore[assignment]

    assert result["ssh"]["healthy"] is False
    assert "connection refused" in result["ssh"]["error"]
    # The path that carries the call still answered.
    assert result["healthy"] is True


def test_a_vm_outside_the_allowlist_is_refused(tmp_path: Path) -> None:
    with pytest.raises(HyperVError, match="is not allowed"):
        service(tmp_path).guest_transport_status("OUTSIDE")


def test_the_status_respects_the_per_vm_transport_override(tmp_path: Path) -> None:
    """HYPERV_MCP_GUEST_TRANSPORT_<VM> is what the page's toggle writes."""
    host = service(
        tmp_path, guest_transport_overrides={VM.casefold(): "psdirect"}, guest_transport="ssh"
    )
    result = host.guest_transport_status(VM, probe=False)
    assert result["configured"] == "psdirect"


def test_the_status_needs_guest_access(tmp_path: Path) -> None:
    host = service(tmp_path, allow_guest=False)
    with pytest.raises(PolicyError, match="HYPERV_MCP_ALLOW_GUEST"):
        host.guest_transport_status(VM)


# -- the switch ----------------------------------------------------------

def test_the_switch_saves_a_choice_the_next_call_uses(tmp_path: Path) -> None:
    """The toggle is a service call, so what it writes is what selection reads."""
    host = service(tmp_path)

    saved = host.guest_transport_set(VM, "psdirect")

    assert saved["transport"] == "psdirect"
    assert saved["saved"] == "psdirect"
    assert Path(saved["path"]) == tmp_path / "transport.json"
    assert host.guest_transport_status(VM, probe=False)["configured"] == "psdirect"
    # A second process reading the same file sees it: the choice outlives the window.
    again = service(tmp_path).guest_transport_status(VM, probe=False)
    assert again["configured"] == "psdirect"
    assert again["saved"] == "psdirect"


def test_auto_removes_the_record_rather_than_saving_a_third_value(tmp_path: Path) -> None:
    host = service(tmp_path)
    host.guest_transport_set(VM, "psdirect")

    saved = host.guest_transport_set(VM, "auto")

    assert saved["saved"] == "auto"
    assert service(tmp_path).guest_transport_status(VM, probe=False)["configured"] == "auto"
    assert "psdirect" not in (tmp_path / "transport.json").read_text(encoding="utf-8")


def test_the_switch_refuses_a_transport_that_is_not_one(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="auto, psdirect, or ssh"):
        service(tmp_path).guest_transport_set(VM, "rdp")


def test_a_per_vm_environment_override_still_wins(tmp_path: Path) -> None:
    """The saved choice is reported, and the environment is named as the reason it loses."""
    host = service(
        tmp_path, guest_transport_overrides={VM.casefold(): "ssh"}, guest_transport="psdirect"
    )

    saved = host.guest_transport_set(VM, "psdirect")

    assert saved["environment"] == "ssh"
    assert host.guest_transport_status(VM, probe=False)["configured"] == "ssh"


def test_a_saved_choice_survives_a_row_that_makes_no_sense(tmp_path: Path) -> None:
    path = tmp_path / "transport.json"
    path.write_text(
        json.dumps({"version": 1, "vms": {VM.casefold(): {"transport": "rdp"},
                                         "other": {"transport": "ssh"}}}),
        encoding="utf-8",
    )

    result = service(tmp_path).guest_transport_status(VM, probe=False)

    # The bad row is ignored; the good one still counts.
    assert result["saved"] is None
    assert result["configured"] == "auto"
    assert "other" in path.read_text(encoding="utf-8")


def test_an_unreadable_transport_file_is_loud(tmp_path: Path) -> None:
    (tmp_path / "transport.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationError, match="could not be read"):
        service(tmp_path).guest_transport_status(VM, probe=False)
