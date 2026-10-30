"""The first-run wizard: three questions, and what counts as already answered.

A window on a host where nothing has been configured can list every virtual
machine and do nothing with any of them, because a VM becomes reachable only
when the operator gives it a level. That is the gap this module fills: pick the
VM, say what the AI may do with it, and store the guest password once, instead
of sending a first-time operator to a JSON file.

The page draws the form; the decisions are here. ``needed`` is derived from the
operator's own files rather than from a flag written when the wizard finishes: a
flag would keep saying "done" on a machine where the level was later removed,
and the wizard would never come back for the VM that still has none. What is
configured is what is done.

Nothing here reads the guest, the vault or the disk. The caller passes what the
service and the credential store already know, which is what makes every case
below a plain test.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..access import LEVELS

# What each level means in the words a first-time operator needs, rather than
# the words the policy file uses. The choice is still the operator's file's; this
# is only the sentence next to the button.
LEVEL_MEANINGS = {
    "read-only": "Look at the VM: its state and screenshots. Nothing on it changes.",
    "control": (
        "Power, checkpoints, the console, screenshots and the DVD drive. "
        "No command runs inside the guest."
    ),
    "lab": (
        "Everything control has, plus commands inside the guest and the kernel "
        "debugger. The level a driver lab needs."
    ),
}

# The questions, in the only order that works: a password is useless before
# there is a VM to spend it on, and the level is what makes the VM reachable at
# all. The password is the one step that may be skipped, because a VM can be
# driven from the host without anything running inside it.
STEPS = ("vm", "level", "credential")

def level_choices() -> list[dict[str, str]]:
    """The levels as the page offers them, in the order the policy declares."""
    return [{"name": level, "meaning": LEVEL_MEANINGS[level]} for level in LEVELS]

def status(
    *,
    vms: Sequence[Mapping[str, Any]],
    levels: Mapping[str, str],
    credentials: Mapping[str, str],
    username: str = "",
) -> dict[str, Any]:
    """What the wizard would ask now, and whether it should open by itself.

    ``levels`` and ``credentials`` are keyed by the casefolded VM name, the way
    the settings and the access file key them. ``credentials`` carries the
    source word (``credential_manager``, ``environment`` or ``missing``), never
    a secret.
    """
    choices: list[dict[str, Any]] = []
    for vm in vms:
        name = str(vm.get("name") or "").strip()
        if not name:
            continue
        level = levels.get(name.casefold()) or None
        source = credentials.get(name.casefold()) or "missing"
        choices.append({
            "name": name,
            "state": str(vm.get("state") or ""),
            "level": level,
            "credential": source,
            "ready": level is not None,
        })

    reachable = [choice for choice in choices if choice["ready"]]
    stored = [choice for choice in choices if choice["credential"] != "missing"]
    blocked = "" if choices else (
        "Hyper-V reported no virtual machine. Create one in Hyper-V Manager, then "
        "open this window again and it will be offered here."
    )
    # No VM to offer is not a first run: there is nothing to ask about, and a
    # form that cannot be finished on every open is noise rather than a welcome.
    needed = bool(choices) and not reachable

    steps = [
        {
            "key": "vm", "title": "Choose the virtual machine",
            "hint": "The list is what Hyper-V reports, not a file this tool keeps.",
            "done": bool(choices),
        },
        {
            "key": "level", "title": "Say what the AI may do with it",
            "hint": "A VM with no level cannot be reached by any tool, from this window or from the AI.",
            "done": bool(reachable),
        },
        {
            "key": "credential", "title": "Store the guest password once",
            "hint": "Optional: only needed to run commands inside the guest. It goes to the Windows Credential Manager, never to a file or to this page.",
            "done": bool(stored),
        },
    ]

    if needed:
        message = (
            "This window can list virtual machines but cannot use any of them yet: "
            "none has a level. Three answers and it can start."
        )
    elif reachable:
        message = f"{len(reachable)} of {len(choices)} virtual machines can be reached."
    else:
        message = blocked

    return {
        "needed": needed,
        "blocked": blocked,
        "message": message,
        "choices": choices,
        "steps": steps,
        "levels": level_choices(),
        "username": username,
    }

__all__ = ["LEVEL_MEANINGS", "STEPS", "level_choices", "status"]
