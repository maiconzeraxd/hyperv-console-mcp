"""Guest credentials: vault resolution, fallback, and the generated password."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from hyperv_console_mcp import credentials
from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import PolicyError

VM = "LAB-Test"


def settings(tmp_path: Path, **overrides) -> Settings:
    values = {"allowed_vms": frozenset({VM}), "allow_guest": True, "ssh_root": tmp_path / "ssh"}
    values.update(overrides)
    return Settings(**values)


def test_the_target_is_namespaced_by_vm() -> None:
    assert credentials.target_name(VM) == "HyperVConsoleMCP/LAB-Test"


def test_the_store_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "_read", lambda target: ("labadmin", "from-vault"))
    value = settings(Path("."), guest_username="envuser", guest_password="from-env")
    assert credentials.resolve(value, VM) == ("labadmin", "from-vault")
    assert credentials.source(value, VM) == "credential_manager"


def test_the_environment_is_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "_read", lambda target: None)
    value = settings(Path("."), guest_username="envuser", guest_password="from-env")
    assert credentials.resolve(value, VM) == ("envuser", "from-env")
    assert credentials.source(value, VM) == "environment"


def test_a_missing_credential_names_both_ways_to_set_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(credentials, "_read", lambda target: None)
    value = settings(Path("."))
    assert credentials.source(value, VM) == "missing"
    with pytest.raises(PolicyError, match="guest_credential_set"):
        credentials.resolve(value, VM)


def test_an_empty_stored_password_does_not_win(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "_read", lambda target: ("labadmin", ""))
    value = settings(Path("."), guest_username="envuser", guest_password="from-env")
    assert credentials.resolve(value, VM) == ("envuser", "from-env")


def test_a_generated_password_satisfies_the_complexity_policy() -> None:
    for _ in range(50):
        password = credentials.generate_password()
        assert len(password) == credentials.PASSWORD_LENGTH
        assert any(character.isupper() for character in password)
        assert any(character.islower() for character in password)
        assert any(character.isdigit() for character in password)
        assert any(character in credentials._SYMBOLS for character in password)
        # Nothing that needs quoting in PowerShell, cmd or the logon dialog.
        assert not set(password) & set("\"'`$; \t\r\n")


def test_generated_passwords_differ() -> None:
    assert len({credentials.generate_password() for _ in range(20)}) == 20


def test_a_short_generated_password_is_refused() -> None:
    with pytest.raises(PolicyError, match="at least 16"):
        credentials.generate_password(12)


@pytest.mark.skipif(sys.platform != "win32", reason="The Credential Manager is Windows only.")
def test_the_windows_vault_round_trips() -> None:
    target = credentials.target_name("hvmcp-test-" + uuid.uuid4().hex)
    try:
        credentials._write(target, "labadmin", "s3cret-value")
        assert credentials._read(target) == ("labadmin", "s3cret-value")
    finally:
        assert credentials._delete(target) is True
    assert credentials._read(target) is None
    assert credentials._delete(target) is False
