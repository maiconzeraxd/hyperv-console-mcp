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


def _report(entries: dict[str, tuple[str, str]]) -> dict[str, Any]:
    return {
        "access_file": str(_store().path),
        "vms": [
            {"name": name, "level": level, "capabilities": sorted(LEVEL_CAPABILITIES[level])}
            for name, level in sorted(entries.values())
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="hyperv-access", description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="Show every VM's level and what it allows.")
    setting = sub.add_parser("set", help="Give a VM a level.")
    setting.add_argument("vm")
    setting.add_argument("level", choices=LEVELS)
    removing = sub.add_parser("remove", help="Forget a VM, which removes it from the allowlist.")
    removing.add_argument("vm")
    arguments = parser.parse_args()
    store = _store()
    try:
        if arguments.action == "list":
            result = _report(store.load())
        elif arguments.action == "set":
            result = _report(store.set(arguments.vm, arguments.level))
        else:
            result = _report(store.remove(arguments.vm))
    except HyperVError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
