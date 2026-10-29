"""``hyperv-install``: write the MCP client's config and a shortcut to the window.

The command is run by the operator, once, on the host. It touches exactly two
things: the named client's own configuration file, and — when asked — a Start
Menu shortcut to the dashboard. A file it does not understand is refused rather
than rewritten, and every file it changes is copied first, so a wrong guess is
one `Move-Item` away from being undone.

Nothing here is reachable from the MCP surface: an install step an AI could run
would be a way to make itself a permanent client.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .errors import ValidationError

CLIENTS = ("claude", "codex", "cursor")
SERVER_NAME = "hyperv"
SHORTCUT_NAME = "Hyper-V Console MCP.lnk"


def client_config(client: str, environ: Mapping[str, str] | None = None) -> Path:
    """Where each client keeps the file that lists its MCP servers."""
    env = os.environ if environ is None else environ
    home = Path(env.get("USERPROFILE") or Path.home())
    if client == "claude":
        return Path(env.get("APPDATA") or home / "AppData" / "Roaming") / "Claude" / "claude_desktop_config.json"
    if client == "cursor":
        return home / ".cursor" / "mcp.json"
    if client == "codex":
        return home / ".codex" / "config.toml"
    raise ValidationError(f"Unknown client {client!r}; expected one of {', '.join(CLIENTS)}.")


def server_entry(*, uvx: bool = False, profile: str | None = None) -> dict[str, Any]:
    """The one server entry both JSON clients and the TOML section carry."""
    if uvx:
        command, args = "uvx", ["hyperv-console-mcp"]
    else:
        # The interpreter running this install: guaranteed to have the package,
        # and the right one when several Pythons are on the machine.
        command, args = sys.executable, ["-m", "hyperv_console_mcp.server"]
    entry: dict[str, Any] = {"command": command, "args": args}
    if profile:
        entry["env"] = {"HYPERV_MCP_PROFILE": profile}
    return entry


def _backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.{stamp}.bak")
    shutil.copy2(path, backup)
    return backup


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def install_json(client: str, path: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Merge one entry into a ``mcpServers`` map, keeping everything else."""
    if path.is_file():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValidationError(
                f"{path} is not valid JSON ({exc}). Fix or move it, then run this again."
            ) from exc
        if not isinstance(document, dict):
            raise ValidationError(f"{path} must hold a JSON object; refusing to rewrite it.")
    else:
        document = {}
    servers = document.get("mcpServers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise ValidationError(f"{path} has an 'mcpServers' that is not an object; refusing to rewrite it.")
    if servers.get(SERVER_NAME) == entry:
        return {"client": client, "path": str(path), "action": "unchanged"}
    action = "updated" if path.is_file() else "created"
    backup = _backup(path)
    servers[SERVER_NAME] = entry
    document["mcpServers"] = servers
    _write_atomic(path, json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    return {
        "client": client,
        "path": str(path),
        "action": action,
        "backup": str(backup) if backup else None,
    }


def toml_section(entry: dict[str, Any]) -> str:
    """The section as TOML, with no dependency on a TOML writer."""
    lines = [f"[mcp_servers.{SERVER_NAME}]", f"command = {json.dumps(entry['command'])}"]
    lines.append("args = [" + ", ".join(json.dumps(item) for item in entry["args"]) + "]")
    for key, value in (entry.get("env") or {}).items():
        lines.append("")
        lines.append(f"[mcp_servers.{SERVER_NAME}.env]")
        lines.append(f"{key} = {json.dumps(value)}")
    return "\n".join(lines) + "\n"


def install_toml(client: str, path: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Append the section when it is absent; never edit a file we did not write.

    A TOML file is somebody's configuration, and re-serialising it without a
    parser would reorder and lose comments. Appending one section, or refusing
    because it is already there, is the whole job.
    """
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    header = f"[mcp_servers.{SERVER_NAME}]"
    if header in text:
        return {"client": client, "path": str(path), "action": "unchanged"}
    backup = _backup(path)
    separator = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
    _write_atomic(path, text + separator + toml_section(entry))
    return {
        "client": client,
        "path": str(path),
        "action": "updated" if text else "created",
        "backup": str(backup) if backup else None,
    }


def install(client: str, *, uvx: bool = False, profile: str | None = None,
            environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    path = client_config(client, environ)
    entry = server_entry(uvx=uvx, profile=profile)
    if client == "codex":
        return install_toml(client, path, entry)
    return install_json(client, path, entry)


def shortcut_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = Path(env.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / SHORTCUT_NAME


def create_shortcut(path: Path) -> dict[str, Any]:
    """A Start Menu entry that opens the window, made by the shell's own COM object."""
    if os.name != "nt":
        return {"shortcut": None, "action": "skipped", "reason": "Windows only"}
    python = Path(sys.executable)
    windowed = python.with_name("pythonw.exe")
    script = (
        "$shell = New-Object -ComObject WScript.Shell;"
        f"$link = $shell.CreateShortcut({json.dumps(str(path))});"
        f"$link.TargetPath = {json.dumps(str(windowed if windowed.is_file() else python))};"
        '$link.Arguments = "-m hyperv_console_mcp.dashboard";'
        f"$link.WorkingDirectory = {json.dumps(str(Path.home()))};"
        f"$link.Description = {json.dumps('Hyper-V Console MCP')};"
        "$link.Save()"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True,
    )
    if completed.returncode != 0 or not path.is_file():
        return {"shortcut": str(path), "action": "failed", "error": completed.stderr.strip()[:500]}
    return {"shortcut": str(path), "action": "created"}


def main() -> None:
    parser = argparse.ArgumentParser(prog="hyperv-install", description=__doc__)
    parser.add_argument("--client", action="append", choices=CLIENTS, dest="clients",
                        help="Write this client's config; repeat for more than one.")
    parser.add_argument("--uvx", action="store_true",
                        help="Write `uvx hyperv-console-mcp` instead of this interpreter.")
    parser.add_argument("--profile", choices=("safe", "lab"), help="Set HYPERV_MCP_PROFILE for the client.")
    parser.add_argument("--shortcut", action="store_true", help="Also add a Start Menu shortcut.")
    arguments = parser.parse_args()
    if not arguments.clients and not arguments.shortcut:
        parser.error("nothing to do: pass --client and/or --shortcut")
    results: list[dict[str, Any]] = []
    try:
        for client in arguments.clients or ():
            results.append(install(
                client, uvx=arguments.uvx, profile=arguments.profile,
            ))
        if arguments.shortcut:
            results.append(create_shortcut(shortcut_path()))
    except ValidationError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps({"installed": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
