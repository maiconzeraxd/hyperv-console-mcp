"""The external worker is gone: every action it did is a service call.

Two things are asserted here. The first is the audit table: each action
``Start-NorthLabWorker.ps1``/``Invoke-NorthLab.ps1`` performed maps to a method
the service really has, and the mapping is the one written in the docs.

The second is the reason the scripts could be dropped at all — nothing on the
paths they used can stop and wait for a person. That is checked where it is
decided: in the PowerShell the package emits (``-NonInteractive`` on every host,
``-Confirm:$false`` on every destructive cmdlet) and in the credential path (a
``PSCredential`` built from the request, never ``Get-Credential``).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.lab.worker import WORKER_ACTIONS
from hyperv_console_mcp.service import HyperVService

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "hyperv_console_mcp"
DOCS = Path(__file__).resolve().parent.parent / "docs" / "PROJECT.md"
BRIDGE = PACKAGE / "bridge" / "HyperVBridge.ps1"

# What a guest credential may become: the vault's entry, turned into the
# object PowerShell Direct needs. Asking for one is the thing that must never
# happen on a path the service takes.
FORBIDDEN = ("Get-Credential", "Read-Host")

# The cmdlets that change something and would otherwise ask first.
DESTRUCTIVE = re.compile(
    r"\b(Stop-VM|Start-VM|Restart-VM|Suspend-VM|Resume-VM|Checkpoint-VM|"
    r"Restore-VMSnapshot|Remove-VMSnapshot|Set-VMFirmware|Debug-VM|Remove-Item)\b"
)


def _service() -> HyperVService:
    from tests.test_server import FakeBackend

    return HyperVService(FakeBackend(), Settings(allowed_vms=frozenset({"NORTH-Test"})))


def test_every_action_the_scripts_performed_is_a_service_method() -> None:
    """The table is the audit: a name that is not on the façade is a gap."""
    service = _service()

    missing = {
        action: [name for name in methods if not hasattr(service, name)]
        for action, methods in WORKER_ACTIONS.items()
    }
    missing = {action: names for action, names in missing.items() if names}

    assert missing == {}, f"no service method for these worker actions: {missing}"
    # And every action has at least one, so a row cannot be emptied by accident.
    assert all(WORKER_ACTIONS.values())


def test_the_documented_table_matches_the_code() -> None:
    """A mapping that only lives in prose rots; this one is read back."""
    documented = DOCS.read_text(encoding="utf-8")

    missing = [
        f"{action} → {', '.join(methods)}"
        for action, methods in WORKER_ACTIONS.items()
        if f"| {action} |" not in documented or not all(f"`{name}`" in documented for name in methods)
    ]

    assert missing == [], f"docs/PROJECT.md does not carry these rows: {missing}"


# A launch, not a mention: ``powershell`` followed by a flag. Prose about
# PowerShell does not match, and implicit concatenation means a command line
# split across lines arrives here as one string.
LAUNCH = re.compile(r"powershell(?:\.exe)?\s+-", re.IGNORECASE)


def _code_strings(path: Path) -> list[str]:
    """Every string literal in the file except the docstrings.

    Docstrings are documentation: ``lab/diagnostics.py`` explains that
    ``powershell.exe -File`` may prefix output with a BOM, and that sentence is
    not a launch. Only literals that a statement can pass to a process count.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def test_no_powershell_this_package_runs_can_wait_for_a_person() -> None:
    """``-NonInteractive`` is the flag that makes "no prompt in runtime" true."""
    offenders: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        for literal in _code_strings(path):
            if LAUNCH.search(literal) and "-noninteractive" not in literal.casefold():
                offenders.append(f"{path.relative_to(PACKAGE).as_posix()}: {literal[:60]}")

    assert offenders == [], "PowerShell launched without -NonInteractive:\n" + "\n".join(offenders)


def test_the_host_backend_starts_its_powershell_with_the_flag_too() -> None:
    """The host side is an argument list, not a command string, so it is its own
    case: the process that runs every Hyper-V command must not open a dialog."""
    text = (PACKAGE / "backend.py").read_text(encoding="utf-8")

    assert '"-NonInteractive"' in text
    assert '"-NoProfile"' in text





def test_nothing_in_the_package_asks_a_person_for_a_credential() -> None:
    """The password comes from the vault; a prompt is the failure mode."""
    offenders: list[str] = []
    for path in list(PACKAGE.rglob("*.py")) + list(PACKAGE.rglob("*.ps1")):
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            if token in text:
                offenders.append(f"{path.relative_to(PACKAGE).as_posix()}: {token}")

    assert offenders == [], f"these would stop for a person: {offenders}"


@pytest.mark.parametrize("path", [BRIDGE], ids=["bridge"])
def test_every_destructive_cmdlet_in_the_bridge_is_told_not_to_ask(path: Path) -> None:
    """``-Confirm:$false`` where it matters, or the service blocks on a dialog."""
    lines = path.read_text(encoding="utf-8").splitlines()
    offenders = [
        line.strip()
        for line in lines
        # A comment mentioning a cmdlet is not a call to it.
        if not line.strip().startswith("#")
        and DESTRUCTIVE.search(line)
        and "-Confirm:$false" not in line
    ]

    assert offenders == [], "a destructive cmdlet without -Confirm:$false:\n" + "\n".join(offenders)


def test_the_bridge_builds_its_credential_instead_of_asking() -> None:
    """The vault's password becomes a PSCredential; nothing reads a prompt."""
    text = BRIDGE.read_text(encoding="utf-8")

    assert "[PSCredential]::new(" in text
    assert "New-PSSession -VMName $name -Credential $credential" in text
