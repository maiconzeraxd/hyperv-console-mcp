"""Run one guest PowerShell program: upload it, execute it, read its result.

Every guest program in this package follows the same contract: it is uploaded to
a fixed path under ``C:\\ProgramData\\HyperVConsoleMCP``, run with ``-File`` and
the arguments the host builds, and it answers on one line with
``HVMCP_JSON:<base64>`` or ``HVMCP_ERROR:<text>``. Keeping that contract here
means the marker is parsed once, and a program that forgets to answer produces
one error message instead of a different one per caller.

The upload happens on every call. A stale copy of a guest script from an earlier
version of this package would otherwise keep answering in the old format, and a
guest that has been restored from a checkpoint has whatever it had then.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from ..errors import BackendError
from .ssh import ERROR_MARKER, JSON_MARKER

GUEST_PROGRAM_ROOT = r"C:\ProgramData\HyperVConsoleMCP"


def quote(value: str) -> str:
    """A PowerShell single-quoted literal, which is the only quoting `-File` needs."""
    return "'" + value.replace("'", "''") + "'"


def parse_marked_output(output: str, *, what: str, code: str) -> dict[str, Any]:
    """The JSON result a guest program wrote, or the error it reported.

    The last marker wins: a program that prints diagnostics before its answer
    still answers once, and the answer is the line the host came for.
    """
    result: dict[str, Any] | None = None
    for line in output.splitlines():
        if line.startswith(JSON_MARKER):
            result = json.loads(base64.b64decode(line[len(JSON_MARKER):]).decode("utf-8"))
        elif line.startswith(ERROR_MARKER):
            raise BackendError(
                f"The guest rejected {what}: {line[len(ERROR_MARKER):]}", code=code
            )
    if result is None:
        raise BackendError(
            f"The guest program for {what} produced no result: {output.strip()[:400]}",
            code=code,
        )
    return result


def remote_path(script: Path) -> str:
    return f"{GUEST_PROGRAM_ROOT}\\{Path(script).name}"


def run(
    service: Any,
    vm: str,
    transport: Any,
    *,
    script: Path,
    arguments: list[str],
    what: str,
    code: str,
) -> dict[str, Any]:
    """Ship ``script`` to the guest and run it with ``arguments``."""
    remote = remote_path(script)
    transport.put_file(vm, Path(script), remote, overwrite=True)
    command = (
        "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        f"-File {quote(remote)}" + (f" {' '.join(arguments)}" if arguments else "")
    )
    execution = transport.exec(
        vm,
        command,
        timeout=service.settings.timeout_seconds,
        max_output=service.settings.max_output_chars,
    )
    return parse_marked_output(execution.output, what=what, code=code)


__all__ = [
    "GUEST_PROGRAM_ROOT", "parse_marked_output", "quote", "remote_path", "run",
]
