"""``hyperv-access``: set the per-VM access level the server enforces.

Run by the operator, on the host, in the same profile the server uses. Nothing
in the MCP surface can call this: a policy the AI could raise is not a policy.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .access import LEVEL_CAPABILITIES, LEVELS, AccessStore, access_file
from .errors import HyperVError


def _store() -> AccessStore:
    return AccessStore(access_file())


def _report(entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    records = sorted(entries.values(), key=lambda record: str(record["name"]).casefold())
    return {
        "access_file": str(_store().path),
        "vms": [
            {
                "name": str(record["name"]),
                "level": str(record["level"]),
                # What the AI may actually do: the level's set, or the subset the
                # operator kept when they narrowed it.
                "capabilities": sorted(
                    record["capabilities"]
                    if record.get("capabilities") is not None
                    else LEVEL_CAPABILITIES[str(record["level"])]
                ),
            }
            for record in records
        ],
    }


def _notify_service(result: dict[str, Any]) -> None:
    """Tell a running service that the file changed.

    The host reads the policy once at startup, so without this a level set here
    would wait for a restart. A host that is not running is not a problem: the
    next client starts one and it reads the file then.
    """
    from .service.host import ServiceClient
    from .service.remote import ServiceProxy

    try:
        client = ServiceClient()
        if not client.available():
            result["service"] = "not running"
            return
        ServiceProxy(client).reload_policy()
        result["service"] = "reloaded"
    except HyperVError as exc:
        result["service"] = f"not reloaded: {exc}"


def main() -> None:
    parser = argparse.ArgumentParser(prog="hyperv-access", description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="Show every VM's level and what it allows.")
    setting = sub.add_parser("set", help="Give a VM a level, and optionally narrow it.")
    setting.add_argument("vm")
    setting.add_argument("level", choices=LEVELS)
    setting.add_argument(
        "--capabilities", metavar="A,B",
        help="Keep only these capabilities of the level, comma separated. "
        "A list can narrow a level, never widen it.",
    )
    removing = sub.add_parser("remove", help="Forget a VM, which removes it from the allowlist.")
    removing.add_argument("vm")
    arguments = parser.parse_args()
    store = _store()
    try:
        if arguments.action == "list":
            # entries(), not load(): load()'s two-item tuples are a compatibility
            # shape for callers that only want the level, not a report record.
            result = _report(store.entries())
        elif arguments.action == "set":
            narrowed = (
                [item.strip() for item in arguments.capabilities.split(",") if item.strip()]
                if arguments.capabilities is not None
                else None
            )
            result = _report(store.set(arguments.vm, arguments.level, narrowed))
            _notify_service(result)
        else:
            result = _report(store.remove(arguments.vm))
            _notify_service(result)
    except HyperVError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
