"""The service is split by domain; this pins the split itself.

Behaviour is covered by the domain tests (`test_service`, `test_lab`,
`test_kd_service`, `test_transport`, `test_ssh_transport`, ...). What this file
checks is the shape the split promised: one façade, one home per method, and no
domain reaching sideways into another.
"""

from __future__ import annotations

import ast
from pathlib import Path

from hyperv_console_mcp.service import GATED_TOOLS, HyperVService, _base

SERVICE = Path(__file__).resolve().parents[1] / "src" / "hyperv_console_mcp" / "service"

DOMAINS = {
    "vm": "VmService",
    "guest": "GuestService",
    "ssh": "SshService",
    "credentials": "CredentialService",
    "guest_config": "GuestConfigService",
    "lab": "LabService",
    "console": "ConsoleService",
    "media": "MediaService",
    "serial_console": "SerialConsoleService",
    "debug_setup": "DebugSetupService",
    "debugger": "DebuggerService",
}


def _module(name: str) -> ast.Module:
    return ast.parse((SERVICE / f"{name}.py").read_text(encoding="utf-8"))


def _methods(name: str) -> set[str]:
    return {
        node.name
        for klass in ast.walk(_module(name))
        if isinstance(klass, ast.ClassDef)
        for node in klass.body
        if isinstance(node, ast.FunctionDef)
    }


def test_the_facade_composes_every_domain() -> None:
    from hyperv_console_mcp import service

    for module, klass in DOMAINS.items():
        domain = getattr(service, klass)
        assert issubclass(domain, _base.ServiceBase), module
        assert issubclass(HyperVService, domain), module
    # The shared helpers come last, so a domain always wins over the base.
    assert HyperVService.__mro__.index(_base.ServiceBase) > max(
        HyperVService.__mro__.index(getattr(service, klass)) for klass in DOMAINS.values()
    )


def test_every_method_has_exactly_one_home() -> None:
    owners: dict[str, list[str]] = {}
    for name in ["_base", *DOMAINS]:
        for method in _methods(name):
            owners.setdefault(method, []).append(name)
    duplicated = {method: where for method, where in owners.items() if len(where) > 1}
    assert duplicated == {}
    # The façade itself defines nothing: it only composes.
    assert _methods("__init__") == set()


def test_a_domain_never_imports_another_domain() -> None:
    for name in DOMAINS:
        for node in ast.walk(_module(name)):
            if not isinstance(node, ast.ImportFrom) or node.level != 1:
                continue
            targets = {node.module} if node.module else {alias.name for alias in node.names}
            assert targets & set(DOMAINS) == set(), (name, targets)
            assert targets <= {"_base"}, (name, targets)


def test_the_gate_list_has_one_home() -> None:
    defining = [
        name
        for name in ["_base", *DOMAINS, "__init__"]
        if any(
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "GATED_TOOLS"
                for target in node.targets
            )
            for node in _module(name).body
        )
    ]
    assert defining == ["_base"]
    assert GATED_TOOLS == _base.GATED_TOOLS
