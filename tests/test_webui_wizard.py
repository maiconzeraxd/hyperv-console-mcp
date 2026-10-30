"""The first-run wizard: three answers, and the state they leave behind.

A first-time operator has a window that can list every VM and do nothing with
any of them, because a VM becomes reachable only when it has a level. What
stands between those two states is one VM, one level and one password — and
these tests pin down both the questions and the fact that the wizard does not
come back once they have been answered.

Nothing here opens a window: the page draws what these functions return, and
what they return is decided in Python, against a fake service.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.access import LEVELS
from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import PolicyError
from hyperv_console_mcp.webui import wizard
from hyperv_console_mcp.webui.api import Api


class FakeService:
    """The service's side of the wizard, with the failures selectable."""

    def __init__(
        self,
        vms: list[dict[str, Any]] | None = None,
        *,
        list_error: Exception | None = None,
        credential_error: Exception | None = None,
        psdirect: dict[str, Any] | None = None,
    ) -> None:
        self.vms = vms if vms is not None else [{"name": "Lab-VM", "state": "Running"}]
        self.list_error = list_error
        self.credential_error = credential_error
        self.psdirect = psdirect if psdirect is not None else {"ok": True, "error": None}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_vms(self) -> dict[str, Any]:
        self.calls.append(("list_vms", {}))
        if self.list_error:
            raise self.list_error
        return {"vms": list(self.vms)}

    def reload_policy(self) -> dict[str, Any]:
        self.calls.append(("reload_policy", {}))
        return {"levels": {}, "allowlist": []}

    def guest_credential_set(self, vm_name: str, username: str, password: str) -> dict[str, Any]:
        self.calls.append((
            "guest_credential_set",
            {"vm_name": vm_name, "username": username, "password": password},
        ))
        if self.credential_error:
            raise self.credential_error
        return {"vm": vm_name, "source": "credential_manager", "username": username}

    def guest_credential_status(self, vm_name: str) -> dict[str, Any]:
        self.calls.append(("guest_credential_status", {"vm_name": vm_name}))
        return {"vm": vm_name, "source": "credential_manager", "psdirect": self.psdirect}

@pytest.fixture(autouse=True)
def _no_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """The vault is the host's; a test says what it holds instead of reading it."""
    monkeypatch.setattr(
        "hyperv_console_mcp.credentials.source",
        lambda _settings, _name: "missing",
    )

def _api(
    tmp_path: Path, *, levels: dict[str, str] | None = None, service: FakeService | None = None
) -> Api:
    settings = Settings(
        allowed_vms=frozenset({"Lab-VM"}),
        levels=dict(levels or {}),
        access_path=tmp_path / "access.json",
    )
    return Api(service or FakeService(), settings, "Lab-VM")

# -- what the wizard asks -------------------------------------------------

def test_a_host_where_no_vm_has_a_level_asks_for_one() -> None:
    plan = wizard.status(
        vms=[{"name": "Lab-VM", "state": "Running"}], levels={}, credentials={}
    )

    assert plan["needed"] is True
    assert plan["blocked"] == ""
    assert "none has a level" in plan["message"]
    assert plan["choices"] == [{
        "name": "Lab-VM", "state": "Running", "level": None,
        "credential": "missing", "ready": False,
    }]

def test_one_vm_with_a_level_is_enough_to_stop_asking() -> None:
    plan = wizard.status(
        vms=[{"name": "Lab-VM", "state": "Off"}, {"name": "Other-VM", "state": "Off"}],
        levels={"lab-vm": "control"},
        credentials={},
    )

    assert plan["needed"] is False
    assert plan["message"] == "1 of 2 virtual machines can be reached."
    assert [choice["ready"] for choice in plan["choices"]] == [True, False]

def test_a_host_with_no_vm_at_all_does_not_open_the_wizard() -> None:
    """A form that cannot be finished on every open is noise, not a welcome."""
    plan = wizard.status(vms=[], levels={}, credentials={})

    assert plan["needed"] is False
    assert "no virtual machine" in plan["blocked"]
    assert plan["message"] == plan["blocked"]

def test_the_questions_are_asked_in_the_order_that_works() -> None:
    plan = wizard.status(vms=[{"name": "Lab-VM"}], levels={}, credentials={})

    assert tuple(step["key"] for step in plan["steps"]) == wizard.STEPS
    # A password is useless before there is a VM to spend it on, and the level
    # is what makes the VM reachable at all.
    assert [step["done"] for step in plan["steps"]] == [True, False, False]

def test_a_level_matches_its_vm_whatever_the_case() -> None:
    """Hyper-V names are case-insensitive, and the two maps are keyed folded."""
    plan = wizard.status(
        vms=[{"name": "LAB-vm"}], levels={"lab-vm": "lab"}, credentials={"lab-vm": "environment"}
    )

    assert plan["choices"][0]["level"] == "lab"
    assert plan["choices"][0]["credential"] == "environment"
    assert plan["choices"][0]["ready"] is True

def test_the_levels_are_offered_with_a_sentence_each() -> None:
    plan = wizard.status(vms=[], levels={}, credentials={})

    assert [level["name"] for level in plan["levels"]] == list(LEVELS)
    assert all(level["meaning"].strip() for level in plan["levels"])

def test_the_answer_carries_a_source_and_never_a_secret() -> None:
    plan = wizard.status(
        vms=[{"name": "Lab-VM"}], levels={}, credentials={"lab-vm": "credential_manager"}
    )

    assert set(plan["choices"][0]) == {"name", "state", "level", "credential", "ready"}
    assert plan["choices"][0]["credential"] == "credential_manager"

# -- the window's side ----------------------------------------------------

def test_the_wizard_reads_the_vm_list_from_the_service(tmp_path: Path) -> None:
    service = FakeService(vms=[{"name": "Lab-VM", "state": "Off"}])
    plan = _api(tmp_path, service=service).wizard()["ok"]

    assert ("list_vms", {}) in service.calls
    assert plan["needed"] is True
    assert plan["username"] == ""

def test_a_service_that_will_not_answer_is_reported_not_swallowed(tmp_path: Path) -> None:
    """The page shows the same envelope it shows for any failed call."""
    api = _api(tmp_path, service=FakeService(list_error=PolicyError("access denied")))

    assert api.wizard()["error"]["code"] == "not_authorized"

def test_a_vm_that_already_has_a_level_does_not_need_the_wizard(tmp_path: Path) -> None:
    assert _api(tmp_path, levels={"lab-vm": "lab"}).wizard()["ok"]["needed"] is False

# -- finishing it ---------------------------------------------------------

def test_the_wizard_writes_the_level_and_the_password_where_they_belong(
    tmp_path: Path,
) -> None:
    service = FakeService()
    api = _api(tmp_path, service=service)

    result = api.wizard_finish("Lab-VM", "control", "Administrator", "s3cret-pass")["ok"]

    stored = json.loads((tmp_path / "access.json").read_text(encoding="utf-8"))
    assert stored["vms"]["lab-vm"] == {"name": "Lab-VM", "level": "control"}
    assert ("guest_credential_set", {
        "vm_name": "Lab-VM", "username": "Administrator", "password": "s3cret-pass",
    }) in service.calls
    assert result["credential"] == "stored"
    assert result["level"] == "control"
    # The password went in and did not come back out, here or in the answer.
    assert "s3cret-pass" not in json.dumps(result)

def test_the_wizard_can_be_finished_without_a_password(tmp_path: Path) -> None:
    """A VM can be driven from the host without anything running inside it."""
    service = FakeService()
    result = _api(tmp_path, service=service).wizard_finish("Lab-VM", "read-only")["ok"]

    assert result["credential"] == "skipped"
    assert result["username"] is None
    assert [name for name, _ in service.calls] == ["reload_policy"]
    assert json.loads((tmp_path / "access.json").read_text(encoding="utf-8"))["vms"]

def test_a_level_the_file_does_not_know_stops_the_wizard_at_that_step(
    tmp_path: Path,
) -> None:
    result = _api(tmp_path).wizard_finish("Lab-VM", "god-mode")

    assert result["error"]["step"] == "level"
    assert result["error"]["code"] == "invalid_argument"
    assert not (tmp_path / "access.json").exists()

def test_a_credential_the_service_refuses_names_the_credential_step(
    tmp_path: Path,
) -> None:
    """The level stays written: it is true, and the operator can retry the rest."""
    service = FakeService(credential_error=PolicyError("no Credential Manager here"))
    result = _api(tmp_path, service=service).wizard_finish("Lab-VM", "lab", "Administrator", "pw")

    assert result["error"]["step"] == "credential"
    assert json.loads((tmp_path / "access.json").read_text(encoding="utf-8"))["vms"]["lab-vm"]["level"] == "lab"

def test_finishing_points_the_window_at_the_vm_it_configured(tmp_path: Path) -> None:
    api = _api(tmp_path)
    api.wizard_finish("Lab-VM", "control")

    assert api.vm_name == "Lab-VM"
    assert api.hello()["level"] == "control"

def test_the_wizard_does_not_come_back_once_a_vm_has_a_level(tmp_path: Path) -> None:
    """The flag that is not written: what is configured is what is done."""
    api = _api(tmp_path)
    assert api.wizard()["ok"]["needed"] is True

    api.wizard_finish("Lab-VM", "control")

    assert api.wizard()["ok"]["needed"] is False

def test_removing_the_level_brings_the_wizard_back(tmp_path: Path) -> None:
    """A level removed by hand is a machine with nothing configured again."""
    api = _api(tmp_path)
    api.wizard_finish("Lab-VM", "control")

    api.remove_level()

    assert api.wizard()["ok"]["needed"] is True

def test_a_wizard_with_no_vm_chosen_names_the_step(tmp_path: Path) -> None:
    assert _api(tmp_path).wizard_finish("  ", "lab")["error"]["step"] == "vm"

def test_storing_the_password_can_be_followed_by_the_proof_it_works(
    tmp_path: Path,
) -> None:
    """The status call is the same one the Guest page makes; the wizard adds none."""
    service = FakeService(psdirect={"ok": False, "error": "the guest did not answer"})
    api = _api(tmp_path, service=service)

    api.wizard_finish("Lab-VM", "lab", "Administrator", "pw")
    status = api.call("guest_credential_status", {"vm_name": "Lab-VM"})

    assert status["ok"]["psdirect"]["ok"] is False
    assert ("guest_credential_status", {"vm_name": "Lab-VM"}) in service.calls
