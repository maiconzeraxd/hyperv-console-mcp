"""The file manager: the host roots it may browse, and the guest half of it.

Two rules are worth testing apart from the page that draws them. The host side
is confined to four configured roots, and that confinement has to hold against
a path built by the caller, not only against one the page offered. The guest
side reaches the whole lab machine, so what protects it is the confirmation on
a delete and the guest script's refusal to take a non-empty tree unasked.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import PolicyError, ValidationError
from hyperv_console_mcp.events import EVENTS
from hyperv_console_mcp.service import HyperVService
from hyperv_console_mcp.service.guest import _TransferProgress

VM = "NORTH-Test"


class FakeGuest:
    """The bridge envelope, with a guest-side file tree the tests can read."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.files: dict[str, bytes] = {}

    def invoke(
        self, operation: str, payload: Mapping[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((operation, dict(payload)))
        vm = payload.get("vm")
        if operation == "guest_list_dir":
            return {"vm": vm, "directory": {"path": payload["path"], "entries": [
                {"name": "Packages", "path": payload["path"] + "\\Packages",
                 "type": "directory", "size": None, "last_write_utc": "2026-10-29T00:00:00+00:00"},
                {"name": "run.log", "path": payload["path"] + "\\run.log",
                 "type": "file", "size": 12, "last_write_utc": "2026-10-29T00:00:00+00:00"},
            ]}}
        if operation == "guest_make_dir":
            return {"vm": vm, "directory": {"path": payload["path"], "created": True}}
        if operation == "guest_rename":
            return {"vm": vm, "entry": {
                "path": payload["path"] + "." + payload["new_name"],
                "previous_path": payload["path"],
            }}
        if operation == "guest_delete":
            return {"vm": vm, "deleted": {
                "path": payload["path"], "type": "directory",
                "recursive": bool(payload.get("recursive")),
            }}
        if operation == "guest_put_file":
            content = Path(payload["local_path"]).read_bytes()
            self.files[payload["guest_path"]] = content
            return {"vm": vm, "file": {
                "path": payload["guest_path"], "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }}
        return {"vm": vm, "operation": operation}


@pytest.fixture(autouse=True)
def empty_vault(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "hyperv_console_mcp.credentials._read", lambda target: None, raising=False
    )
    monkeypatch.setenv("HYPERV_MCP_EVENT_DIRECTORY", str(tmp_path / "events"))


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    for name in ("lab", "evidence", "transfer", "media"):
        (tmp_path / name).mkdir()
    return {name: tmp_path / name for name in ("lab", "evidence", "transfer", "media")}


@pytest.fixture
def service(roots: dict[str, Path]) -> HyperVService:
    return HyperVService(
        FakeGuest(),
        Settings(
            allowed_vms=frozenset({VM}),
            allow_guest=True,
            guest_username="Administrator",
            guest_password="secret",
            lab_root=roots["lab"],
            evidence_root=roots["evidence"],
            transfer_root=roots["transfer"],
            iso_root=roots["media"],
        ),
    )


# -- the host side -------------------------------------------------------


def test_file_roots_names_the_four_roots_and_whether_they_exist(
    service: HyperVService, roots: dict[str, Path]
) -> None:
    listed = service.file_roots()["roots"]
    assert [root["name"] for root in listed] == ["lab", "evidence", "transfer", "media"]
    assert all(root["exists"] is True for root in listed)
    assert str(roots["lab"]) in {root["path"] for root in listed}


def test_host_list_dir_puts_directories_first(service: HyperVService, roots: dict[str, Path]) -> None:
    (roots["lab"] / "zeta.txt").write_text("x", encoding="utf-8")
    (roots["lab"] / "Alpha").mkdir()
    (roots["lab"] / "beta").mkdir()

    listed = service.host_list_dir("lab")
    assert listed["root"] == "lab"
    assert listed["directory"]["path"] == ""
    assert [entry["name"] for entry in listed["directory"]["entries"]] == [
        "Alpha", "beta", "zeta.txt",
    ]
    assert [entry["type"] for entry in listed["directory"]["entries"]] == [
        "directory", "directory", "file",
    ]
    assert listed["directory"]["entries"][2]["size"] == 1


def test_a_path_that_climbs_out_of_its_root_is_refused(
    service: HyperVService, tmp_path: Path
) -> None:
    with pytest.raises(PolicyError, match="must remain under"):
        service.host_list_dir("lab", "..")
    with pytest.raises(PolicyError, match="must remain under"):
        service.host_list_dir("lab", str(tmp_path / "evidence"))
    with pytest.raises(ValidationError, match="root must be one of"):
        service.host_list_dir("C:\\", "")


def test_host_make_dir_is_idempotent_and_says_whether_it_created(
    service: HyperVService, roots: dict[str, Path]
) -> None:
    first = service.host_make_dir("lab", "package")
    second = service.host_make_dir("lab", "package")
    assert first["directory"] == {"path": "package", "created": True}
    assert second["directory"] == {"path": "package", "created": False}
    assert (roots["lab"] / "package").is_dir()


def test_host_rename_stays_in_the_parent_and_refuses_an_existing_target(
    service: HyperVService, roots: dict[str, Path]
) -> None:
    (roots["lab"] / "old.txt").write_text("x", encoding="utf-8")
    (roots["lab"] / "taken.txt").write_text("y", encoding="utf-8")

    renamed = service.host_rename("lab", "old.txt", "new.txt")
    assert renamed["entry"] == {"path": "new.txt", "previous_path": "old.txt"}
    assert (roots["lab"] / "new.txt").is_file()

    with pytest.raises(ValidationError, match="already exists"):
        service.host_rename("lab", "new.txt", "taken.txt")
    with pytest.raises(ValidationError, match="single file or directory name"):
        service.host_rename("lab", "new.txt", "..\\escape")


def test_host_delete_needs_the_path_spelled_back(
    service: HyperVService, roots: dict[str, Path]
) -> None:
    target = roots["lab"] / "run.log"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(PolicyError, match="confirm_path"):
        service.host_delete("lab", "run.log", "lab/run.log")
    assert target.is_file()

    deleted = service.host_delete("lab", "run.log", "run.log")
    assert deleted["deleted"]["type"] == "file"
    assert not target.exists()


def test_host_delete_refuses_a_non_empty_directory_without_recursive(
    service: HyperVService, roots: dict[str, Path]
) -> None:
    tree = roots["lab"] / "package"
    tree.mkdir()
    (tree / "driver.sys").write_bytes(b"MZ")

    with pytest.raises(OSError):
        service.host_delete("lab", "package", "package")
    assert (tree / "driver.sys").is_file()

    service.host_delete("lab", "package", "package", recursive=True)
    assert not tree.exists()


def test_the_root_itself_is_never_renamed_or_deleted(
    service: HyperVService, roots: dict[str, Path]
) -> None:
    with pytest.raises(ValidationError, match="root itself"):
        service.host_delete("lab", "", "")
    with pytest.raises(ValidationError, match="root itself"):
        service.host_rename("lab", "", "other")


# -- the guest side ------------------------------------------------------


def test_guest_list_dir_asks_the_guest_for_its_listing(service: HyperVService) -> None:
    listed = service.guest_list_dir(VM, r"C:\DriverLab")
    assert listed["directory"]["path"] == r"C:\DriverLab"
    assert [entry["name"] for entry in listed["directory"]["entries"]] == [
        "Packages", "run.log",
    ]
    assert listed["transport"] == "psdirect"


def test_guest_make_dir_and_rename_carry_the_path_to_the_guest(service: HyperVService) -> None:
    made = service.guest_make_dir(VM, r"C:\DriverLab\new")
    assert made["directory"] == {"path": r"C:\DriverLab\new", "created": True}
    renamed = service.guest_rename(VM, r"C:\DriverLab\new", "renamed")
    assert renamed["entry"]["previous_path"] == r"C:\DriverLab\new"


def test_guest_delete_needs_the_path_spelled_back(service: HyperVService) -> None:
    with pytest.raises(PolicyError, match="confirm_path"):
        service.guest_delete(VM, r"C:\DriverLab\old", r"C:\DriverLab")
    deleted = service.guest_delete(VM, r"C:\DriverLab\old", r"C:\DriverLab\old", recursive=True)
    assert deleted["deleted"] == {
        "path": r"C:\DriverLab\old", "type": "directory", "recursive": True,
    }


def test_guest_delete_refuses_a_non_boolean_recursive(service: HyperVService) -> None:
    with pytest.raises(ValidationError, match="recursive"):
        service.guest_delete(VM, r"C:\DriverLab\old", r"C:\DriverLab\old", recursive="yes")  # type: ignore[arg-type]


# -- progress ------------------------------------------------------------


def test_a_transfer_reports_its_bytes_to_the_event_feed(
    service: HyperVService, roots: dict[str, Path]
) -> None:
    source = roots["lab"] / "driver.sys"
    source.write_bytes(b"MZ" + bytes(4096))
    before = EVENTS.snapshot()["latest_sequence"]

    service.guest_put_file(VM, str(source), r"C:\DriverLab\driver.sys")

    events = [
        event for event in EVENTS.snapshot(before)["events"]
        if event["operation"] == "guest_put_file" and event["phase"] == "progress"
    ]
    assert events, "a transfer must say how far it got"
    assert [event["progress"] for event in events] == sorted(
        event["progress"] for event in events
    )
    assert all(5 <= event["progress"] <= 99 for event in events)
    assert events[-1]["progress"] == 99
    assert events[-1]["message"].startswith("verified ")
    # The digest is public; the bytes are not, and neither is the password.
    assert all("secret" not in str(event) for event in events)


def test_transfer_progress_is_throttled_to_whole_percents() -> None:
    """A block-by-block callback must not become a block-by-block event."""
    reporter = _TransferProgress("guest_put_file", VM, 1000)
    before = EVENTS.snapshot()["latest_sequence"]

    for done in range(0, 1001, 5):  # 201 callbacks, as an SFTP put would send
        reporter(done, 1000)

    published = [
        event["progress"] for event in EVENTS.snapshot(before)["events"]
        if event["operation"] == "guest_put_file" and event["phase"] == "progress"
    ]
    assert 0 < len(published) <= 86
    assert published == sorted(set(published))
    assert published[-1] == 90, "the band has to leave room for the verification"

