"""Minimal audit metadata, deliberately excluding commands, text, and secrets."""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path


class AuditLogger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(self, operation: str, vm_name: str | None, outcome: str, code: str = "") -> None:
        if self.path is None:
            return
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "operation": operation,
            "vm": vm_name,
            "outcome": outcome,
        }
        if code:
            event["error_code"] = code
        try:
            with self._lock, self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=True) + "\n")
        except OSError as exc:
            # Audit failure must not corrupt MCP stdio or hide the tool's actual result.
            print(f"hyperv-console-mcp: audit write failed: {exc}", file=sys.stderr)
