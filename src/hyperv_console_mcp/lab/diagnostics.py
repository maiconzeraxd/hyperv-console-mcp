"""The lab diagnostic view, read on the host from the guest's raw facts.

The guest script collects; it does not conclude. It returns registry values,
the tokens bcdedit printed and the figures it measured, and this module turns
them into the answers a reader wants — "is test signing on", "is the debugger
enabled" — so the reading is testable without a VM and exists once.

Nothing here touches a VM: ``derive_diagnostics`` is a pure function of the
document the guest printed.
"""

from __future__ import annotations

import json
from typing import Any

from ..bcd import bcd_boolean


def derive_diagnostics(facts: dict[str, Any]) -> dict[str, Any]:
    """Add the two boot verdicts to the guest's facts, leaving the facts intact.

    The tokens stay in the answer: a reader who disagrees with a verdict can see
    what it was made from.
    """
    boot = facts.get("boot_tokens") or {}
    return {
        **facts,
        "test_signing": bcd_boolean(boot.get("testsigning")),
        "debug_enabled": bcd_boolean(boot.get("debug")),
    }


def parse_report(output: str) -> dict[str, Any] | None:
    """The JSON document the diagnostics script printed, or ``None``.

    The script writes one compressed object, but `powershell.exe -File` may put
    a warning line ahead of it on a guest with an unusual profile, so a line of
    its own that parses as an object is accepted too.
    """
    text = (output or "").strip()
    if not text:
        return None
    for candidate in (text, *reversed(text.splitlines())):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None
