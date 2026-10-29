"""The lab diagnostics report: the guest collects, the host concludes."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

from hyperv_console_mcp.bcd import bcd_boolean
from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import BackendError
from hyperv_console_mcp.lab.cli import diagnostics
from hyperv_console_mcp.lab.diagnostics import derive_diagnostics, parse_report
from hyperv_console_mcp.transport.ssh import JSON_MARKER

VM = "NORTH-Test"

# What the guest script prints on a pt-BR guest with test signing on and the
# debugger off. bcdedit translates the labels around an element, never the
# value, so these tokens are the same in every display language.
GUEST_REPORT = {
    "collected_utc": "2026-10-29T12:00:00.0000000Z",
    "computer_name": "DESKTOP-AA9PSTJ",
    "hypervisor_present": True,
    "windows_caption": "Microsoft Windows 10 Pro",
    "windows_version": "10.0.19045",
    "last_boot_utc": "2026-10-29T11:00:00.0000000Z",
    "free_system_drive_bytes": 40 * 1024**3,
    "secure_boot": False,
    "boot_tokens": {"testsigning": "Yes", "debug": "No"},
    "debug_settings": "Identificador           {dbgsettings}\n",
    "vbs_status": 0,
    "hvci_status": 0,
    "crash_dump_type": 7,
    "crash_dump_file": "%SystemRoot%\\MEMORY.DMP",
    "minidump_directory": "%SystemRoot%\\Minidump",
    "automatic_reboot": True,
    "security_changed": False,
    "payload_executed": False,
}


class StubService:
    """A service that answers one `guest_exec` and records what it was asked."""

    def __init__(self, execution: dict[str, Any]) -> None:
        self.execution = execution
        self.calls: list[tuple[str, str]] = []

    def guest_exec(self, vm_name: str, command: str, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append((vm_name, command))
        return {"vm": vm_name, "execution": self.execution}


def _execution(report: str, *, success: bool = True) -> dict[str, Any]:
    return {"command": "", "success": success, "exit_code": 0 if success else 1,
            "output": report, "error_records": []}


def test_the_guest_reports_tokens_and_the_host_decides_what_they_mean() -> None:
    """Regression: the guest ran `-match 'testsigning\\s+Yes'` and shipped a verdict.

    That made the only reading of the token a regex inside a script that can be
    run only on a VM, and put a second bcdedit parser next to the one in
    guest_config.ps1.
    """
    derived = derive_diagnostics(GUEST_REPORT)

    assert derived["test_signing"] is True
    assert derived["debug_enabled"] is False
    # The facts stay in the answer, so a verdict can be checked against them.
    assert derived["boot_tokens"] == {"testsigning": "Yes", "debug": "No"}
    assert derived["crash_dump_type"] == 7
    assert derived["payload_executed"] is False


def test_an_absent_token_is_off_and_an_unreadable_one_is_not_a_verdict() -> None:
    """bcdedit omits a setting that is off; a token this build cannot read is not off."""
    absent = derive_diagnostics({"boot_tokens": {"testsigning": None}})
    assert absent["test_signing"] is False
    assert absent["debug_enabled"] is False

    unknown = derive_diagnostics({"boot_tokens": {"testsigning": "Sim", "debug": "1"}})
    assert unknown["test_signing"] is None
    assert unknown["debug_enabled"] is None


def test_every_spelling_bcdedit_accepts_reads_the_same_way() -> None:
    for token in ("Yes", "yes", "on", "ON", "true", " True "):
        assert bcd_boolean(token) is True
    for token in ("No", "off", "FALSE"):
        assert bcd_boolean(token) is False


def test_diagnostics_adds_the_derived_view_and_keeps_the_raw_execution() -> None:
    service = StubService(_execution(json.dumps(GUEST_REPORT)))

    result = diagnostics(service, VM)  # type: ignore[arg-type]

    assert service.calls[0][0] == VM
    assert "Get-DriverLabDiagnostics.ps1" in service.calls[0][1]
    assert result["diagnostics"]["test_signing"] is True
    # The raw report is still there for anyone who wants to read it themselves.
    assert json.loads(result["execution"]["output"]) == GUEST_REPORT


def test_a_report_the_host_cannot_read_is_returned_untouched() -> None:
    """A failed run prints its reason, not JSON; that reason must survive."""
    service = StubService(_execution("bcdedit /enum failed: access denied", success=False))

    result = diagnostics(service, VM)  # type: ignore[arg-type]

    assert "diagnostics" not in result
    assert result["execution"]["output"] == "bcdedit /enum failed: access denied"


def test_a_warning_ahead_of_the_report_does_not_hide_it() -> None:
    report = "WARNING: the module could not be loaded\n" + json.dumps(GUEST_REPORT)
    assert parse_report(report) == GUEST_REPORT


class VerifierStubService(StubService):
    """The same service, plus the transport the verifier's own program needs."""

    def __init__(self, execution: dict[str, Any], *, verifier_error: Exception | None = None) -> None:
        super().__init__(execution)
        self.settings = Settings(allowed_vms=frozenset({VM}), allow_guest=True)
        self.verifier_error = verifier_error
        self.transport_calls: list[str] = []

    def _transport(self, vm_name: str) -> "VerifierStubService":
        return self

    def health(self, vm_name: str) -> dict[str, Any]:
        return {"ok": True}

    def put_file(self, vm_name: str, local: Any, remote: str, *, overwrite: bool) -> Any:
        self.transport_calls.append(remote)
        return SimpleNamespace(path=remote, size=0, sha256="0" * 64)

    def exec(self, vm_name: str, command: str, *, timeout: float, max_output: int) -> Any:
        if self.verifier_error is not None:
            raise self.verifier_error
        payload = {
            "mode": "Status",
            "live": {"flags_mask": 1, "drivers": [], "drivers_raw": None, "boot_utc": "2026-10-29T00:00:00Z"},
        }
        body = json.dumps(payload).encode("utf-8")
        output = JSON_MARKER + base64.b64encode(body).decode("ascii")
        return SimpleNamespace(output=output, success=True, exit_code=0)


def test_the_verifier_setting_is_read_beside_the_boot_configuration() -> None:
    """One read tool answers for the whole driver-testing configuration.

    The verifier's own guest program produces the facts, so the flags are read
    in exactly one place and the diagnostics report carries them beside the
    boot entries instead of a second listing tool existing for them.
    """
    service = VerifierStubService(_execution(json.dumps(GUEST_REPORT)))

    result = diagnostics(service, VM)  # type: ignore[arg-type]

    assert result["diagnostics"]["test_signing"] is True
    assert result["verifier"]["enabled"] is True
    assert result["verifier"]["live"]["flags_named"] == ["special_pool"]
    assert service.transport_calls[-1].endswith("guest_verifier.ps1")


def test_a_verifier_that_cannot_answer_keeps_its_error_and_the_rest_of_the_report() -> None:
    """A guest with no verifier must not cost the boot and dump facts beside it."""
    service = VerifierStubService(
        _execution(json.dumps(GUEST_REPORT)),
        verifier_error=BackendError("verifier is not available", code="guest_verifier_failed"),
    )

    result = diagnostics(service, VM)  # type: ignore[arg-type]

    assert result["diagnostics"]["test_signing"] is True
    assert result["verifier"]["error"] == "guest_verifier_failed"
