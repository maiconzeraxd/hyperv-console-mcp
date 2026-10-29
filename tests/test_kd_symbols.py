"""Whether kd resolved a module's symbols: what `lm`/`!lmi` say, and the read."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from hyperv_console_mcp.config import Settings
from hyperv_console_mcp.errors import ValidationError
from hyperv_console_mcp.kd import analyze_report, symbols
from hyperv_console_mcp.service import HyperVService

LMI_PRIVATE = """\
Loaded Module Info: [mydriver]
         Module: mydriver
   Base Address: fffff806`7a2a0000
     Image Name: mydriver.sys
   Machine Type: 34404 (X64)
     Time Stamp: Sat Oct 25 12:00:00 2025 (68FC1A2B)
           Size: 1b000
       CheckSum: 1c000
Characteristics: 22
Debug Data Dirs: Type  Size     VA  Pointer
             CODEVIEW    25,  1a2b0,   19ab0 RSDS - GUID: {1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809}
               Age: 2, Pdb: C:\\build\\out\\mydriver.pdb
   Image Type: MEMORY   - Image read successfully from loaded memory.
  Symbol Type: PDB      - PDB file 'mydriver.pdb' loaded for image.
    Load Report: private symbols & lines, not source indexed
                C:\\Users\\lab\\symbols\\mydriver.pdb\\1A2B3C4D2\\mydriver.pdb
"""

LMI_EXPORT = """\
Loaded Module Info: [mydriver]
         Module: mydriver
   Base Address: fffff806`7a2a0000
     Image Name: mydriver.sys
   Image Type: MEMORY   - Image read successfully from loaded memory.
  Symbol Type: EXPORT   - PDB not found.
    Load Report: export symbols only
"""

LMI_MISMATCH = """\
Loaded Module Info: [mydriver]
         Module: mydriver
     Image Name: mydriver.sys
   Image Type: MEMORY   - Image read successfully from loaded memory.
  Symbol Type: PDB      - PDB file 'mydriver.pdb' loaded for image.
    Load Report: *** Mismatched PDB ***
"""

LM_ROW = """\
start             end                 module name
fffff806`7a2a0000 fffff806`7a2bb000   mydriver   (private pdb symbols)  C:\\sym\\mydriver.pdb\\1A2B3C\\mydriver.pdb
fffff806`7a2bc000 fffff806`7a2c5000   other      (deferred)
"""


def test_a_private_pdb_is_a_loaded_module() -> None:
    result = symbols.parse_lmi(LMI_PRIVATE, "mydriver")
    assert result["found"] is True
    assert result["loaded"] is True
    assert result["symbol_kind"] == "private_pdb"
    assert result["pdb"] == r"C:\build\out\mydriver.pdb"
    assert result["base"] == "0xfffff8067a2a0000"


def test_export_symbols_mean_the_pdb_was_not_found() -> None:
    result = symbols.parse_lmi(LMI_EXPORT, "mydriver")
    assert result["loaded"] is False
    assert result["symbol_kind"] == "export"
    assert "pdb" not in result


def test_a_pdb_that_does_not_match_the_image_is_not_loaded() -> None:
    """kd says "PDB loaded" and then rejects it; the stack has no names either way."""
    result = symbols.parse_lmi(LMI_MISMATCH, "mydriver")
    assert result["loaded"] is False
    assert result["symbol_kind"] == "rejected"
    assert "Mismatched" in result["load_report"]


def test_a_module_kd_has_not_loaded_is_not_found() -> None:
    assert symbols.parse_lmi("No matching modules found.\n", "mydriver")["found"] is False


def test_lm_carries_the_symbol_state_and_the_pdb() -> None:
    result = symbols.parse_lm(LM_ROW, "mydriver")
    assert result["found"] is True
    assert result["loaded"] is True
    assert result["symbol_kind"] == "private_pdb"
    assert result["pdb"] == r"C:\sym\mydriver.pdb\1A2B3C\mydriver.pdb"
    assert result["start"] == "0xfffff8067a2a0000"


def test_lm_reads_a_module_whose_symbols_are_only_deferred() -> None:
    result = symbols.parse_lm(LM_ROW, "other")
    assert result["loaded"] is False
    assert result["symbol_kind"] == "deferred"


def test_another_module_on_the_same_listing_is_not_mistaken_for_this_one() -> None:
    assert symbols.parse_lm(LM_ROW, "mydriverx")["found"] is False


def test_a_module_name_cannot_carry_a_path_or_an_extension() -> None:
    assert symbols.module_name("mydriver.sys") == "mydriver"
    assert symbols.module_name("MyDriver") == "MyDriver"
    for bad in (r"C:\build\mydriver", "my driver", "mydriver;q", "", "a/b"):
        with pytest.raises(ValidationError):
            symbols.module_name(bad)


def test_an_analysis_that_named_a_function_resolved_symbols() -> None:
    resolved = analyze_report("Probably caused by : MyDriver.sys ( MyDriver!Crash+18 )")
    assert resolved["symbols_resolved"] is True
    assert resolved["symbol_name"] == "MyDriver!Crash+18"


def test_an_analysis_that_only_names_an_offset_did_not() -> None:
    """What kd prints when the PDB was missing: the module, then a bare offset."""
    unresolved = analyze_report("Probably caused by : MyDriver.sys ( MyDriver+18 )")
    assert unresolved["symbols_resolved"] is False
    assert unresolved["blamed_driver"] == "MyDriver.sys"


def test_kds_placeholder_for_an_unknown_symbol_counts_as_unresolved() -> None:
    assert analyze_report("Probably caused by : MyDriver.sys ( !name )")["symbols_resolved"] is False


class FakeBackend:
    def invoke(
        self, operation: str, payload: Mapping[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        return {"operation": operation}


class StubSession:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.commands: list[str] = []

    def command(self, text: str, *, timeout: float | None = None) -> dict[str, Any]:
        self.commands.append(text)
        return {"command": text, "output": self.answers.get(text, ""), "state": "broken"}


class StubKd:
    def __init__(self, session: StubSession) -> None:
        self.session = session

    def require(self, vm: str) -> StubSession:
        return self.session


def service_with(tmp_path: Path, session: StubSession) -> HyperVService:
    instance = HyperVService(
        FakeBackend(),
        Settings(
            allowed_vms=frozenset({"NORTH-Test"}),
            allow_debugger=True,
            kd_log_root=tmp_path / "kd",
            symbol_root=tmp_path / "symbols",
            evidence_root=tmp_path / "evidence",
        ),
    )
    instance.kd = StubKd(session)  # type: ignore[assignment]
    return instance


def test_the_debugger_is_asked_about_the_module_and_nothing_else(tmp_path: Path) -> None:
    session = StubSession({"!lmi MyDriver": LMI_PRIVATE})
    result = service_with(tmp_path, session).kd_symbols("NORTH-Test", "MyDriver.sys")
    assert session.commands == ["!lmi MyDriver"]
    assert result["loaded"] is True
    assert result["vm"] == "NORTH-Test"


def test_a_module_lmi_does_not_know_falls_back_to_lm(tmp_path: Path) -> None:
    session = StubSession({"lm m mydriver": LM_ROW})
    result = service_with(tmp_path, session).kd_symbols("NORTH-Test", "mydriver")
    assert session.commands == ["!lmi mydriver", "lm m mydriver"]
    assert result["source"] == "lm"
    assert result["symbol_kind"] == "private_pdb"


def test_a_module_that_is_not_loaded_at_all_says_so(tmp_path: Path) -> None:
    result = service_with(tmp_path, StubSession({})).kd_symbols("NORTH-Test", "mydriver")
    assert result["found"] is False
    assert result["loaded"] is None
