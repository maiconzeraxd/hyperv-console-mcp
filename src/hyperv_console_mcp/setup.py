"""The install routine: what this host needs, and what the installer does about it.

Three doors lead here — the signed installer, ``winget`` and ``install.ps1`` —
and all three end in :func:`first_run`, so there is one answer to "what does an
install do on this machine" and one place to change it. Each step is idempotent:
running it again repairs a partial install instead of duplicating it.

The questions are asked through functions the caller supplies — ``probe``,
``exists``, ``environ`` — rather than by reading the registry and the shell from
inside this module, because the interesting hosts are the ones a developer never
sees: Windows Home, where Hyper-V cannot run at all; a machine where the feature
is installed but switched off; a machine with no MCP client on it. A test
supplies those answers; the installer supplies the real ones.

Every step says what it did in one sentence, and a failure names the step and
the fix. A stack trace is not an answer to "why did the install stop".

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import install
from .access import AccessStore, access_file
from .config import data_root
from .credentials import KDNET_TARGET_PREFIX, CredentialStore, SecretStore
from .errors import HyperVError

# The editions that cannot host Hyper-V at all, so the routine can say so before
# the operator waits through a feature install that was never going to work.
# Everything else — Pro, Enterprise, Education, every Server SKU — can.
HOME_EDITIONS = frozenset({
    "core", "coren", "coresinglelanguage", "corecountry-specific",
    "home", "homen", "homesinglelanguage", "homechina", "starter",
})

# What the operator runs when the feature is installed and switched off. The
# routine prints this exact command rather than describing it, because the
# operator will otherwise have to find the feature's real name.
ENABLE_FEATURE = (
    "Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V -All"
)

# Win32_OptionalFeature is readable without elevation, unlike the DISM cmdlets,
# which is what lets the pre-check run before the one UAC prompt.
FEATURE_QUERY = (
    "$feature = Get-CimInstance -ClassName Win32_OptionalFeature "
    "-Filter \"Name='Microsoft-Hyper-V'\" -ErrorAction Stop; "
    "if ($feature) { $feature.InstallState } else { 3 }"
)

# Win32_OptionalFeature.InstallState.
_INSTALL_STATE = {"1": "enabled", "2": "disabled", "3": "absent"}

Probe = Callable[[str], str]


@dataclass(frozen=True)
class Host:
    """The two things an install has to know about the machine it runs on."""

    edition: str = ""
    product: str = ""
    hyperv: str = "unknown"


def supports_hyperv(edition: str) -> bool:
    """Whether this edition of Windows can run Hyper-V.

    Home cannot, and that is a stop with a sentence rather than an
    ``Enable-WindowsOptionalFeature`` the operator has to decode.
    """
    return edition.strip().casefold() not in HOME_EDITIONS


def assess(host: Host) -> dict[str, Any]:
    """What the installer should do about this host, in one answer.

    ``action`` is what the caller acts on — ``proceed``, ``enable-hyperv`` or
    ``stop`` — and ``message`` is the sentence the operator reads. ``unknown``
    is not treated as a failure: a machine that cannot answer the query still
    gets an install, and the self-test at the end is what decides whether
    Hyper-V works.
    """
    plan: dict[str, Any] = {
        "edition": host.edition,
        "product": host.product,
        "hyperv": host.hyperv,
        "supports_hyperv": True,
        "action": "proceed",
        "command": None,
        "message": "",
    }
    if not host.edition and not host.product:
        plan.update(action="stop", supports_hyperv=False, message=(
            "This installer runs on Windows only, and this machine did not answer as one."
        ))
        return plan
    name = host.product or host.edition
    if not supports_hyperv(host.edition):
        plan.update(action="stop", supports_hyperv=False, message=(
            f"{name} cannot run Hyper-V, so there is nothing here for the console to "
            "control. Windows Pro, Enterprise or Education can; so can every Server "
            "edition. Run this installer on a machine with one of them."
        ))
        return plan
    if host.hyperv == "absent":
        plan.update(action="stop", message=(
            "Hyper-V is not part of this Windows installation. Add it from 'Turn Windows "
            "features on or off' (or run `" + ENABLE_FEATURE + "` in an elevated "
            "PowerShell), restart, and run this installer again."
        ))
        return plan
    if host.hyperv == "disabled":
        plan.update(action="enable-hyperv", command=ENABLE_FEATURE, message=(
            "Hyper-V is installed but switched off. Enabling it needs a restart, and the "
            "install continues after that."
        ))
        return plan
    if host.hyperv == "unknown":
        plan["message"] = (
            "This machine did not report whether Hyper-V is switched on. The install "
            "continues; the check at the end will say whether Hyper-V answers."
        )
        return plan
    plan["message"] = f"{name} has Hyper-V ready."
    return plan


def read_host(probe: Probe | None = None) -> Host:
    """Ask this machine the two questions the routine needs answered."""
    if os.name != "nt":
        return Host()
    edition, product = _edition()
    return Host(edition=edition, product=product, hyperv=_hyperv_state(probe or _run))


def _edition() -> tuple[str, str]:
    """The edition id and the product name, as the machine records them."""
    import winreg

    values = {"EditionID": "", "ProductName": ""}
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        ) as key:
            for name in values:
                try:
                    values[name] = str(winreg.QueryValueEx(key, name)[0])
                except OSError:
                    pass
    except OSError:
        pass
    return values["EditionID"], values["ProductName"]


def _run(script: str) -> str:
    """Run one PowerShell expression and return what it printed, or ``''``."""
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _hyperv_state(probe: Probe) -> str:
    return _INSTALL_STATE.get(probe(FEATURE_QUERY), "unknown")


# -- the MCP clients ------------------------------------------------------

def detect_clients(
    environ: Mapping[str, str] | None = None,
    exists: Callable[[Path], bool] | None = None,
) -> list[str]:
    """Which MCP clients this machine already has, so their config can be written.

    A client is present when its own directory is: a freshly installed Claude
    Desktop has the folder and no config file yet, and writing that file is the
    whole point of noticing it.
    """
    check = exists or (lambda path: path.is_dir())
    return [
        client
        for client in install.CLIENTS
        if check(install.client_config(client, environ).parent)
    ]


def configure_clients(
    clients: list[str] | None = None,
    *,
    uvx: bool = False,
    profile: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Write every detected client's config; hand over the snippet when there is none.

    ``snippet`` is the entry and the file each client keeps it in, so an
    operator whose client was not detected pastes one line instead of reading
    the source to work out what the entry should say.
    """
    chosen = list(detect_clients(environ) if clients is None else clients)
    written = [
        install.install(client, uvx=uvx, profile=profile, environ=environ)
        for client in chosen
    ]
    snippet = None
    if not chosen:
        snippet = {
            "entry": install.server_entry(uvx=uvx, profile=profile),
            "paste_into": {
                client: str(install.client_config(client, environ))
                for client in install.CLIENTS
            },
        }
    return {"clients": chosen, "written": written, "snippet": snippet}


# -- the self-test --------------------------------------------------------

def self_test(service: Any) -> dict[str, Any]:
    """The last line of an install, asked of the service rather than assumed.

    The service answers from its own event bus first, which separates "nothing
    is listening on the pipe" from "the pipe answered and Hyper-V did not" —
    two failures with two different fixes.
    """
    try:
        service.event_snapshot()
    except (HyperVError, OSError) as exc:
        return {"ok": False, "step": "service", "message": (
            f"The service is not answering: {exc} Start it with `hyperv-service`, "
            "or open the window, which starts it for you."
        )}
    try:
        service.host_status()
    except (HyperVError, OSError) as exc:
        return {"ok": False, "step": "hyper-v", "message": (
            f"The service is running, but Hyper-V did not answer: {exc} Check that "
            "Hyper-V is switched on and that this account is in the Hyper-V "
            "Administrators group."
        )}
    try:
        vms = list(service.list_vms().get("vms") or [])
    except (HyperVError, OSError) as exc:
        return {"ok": False, "step": "hyper-v", "message": (
            f"The service is running, but it could not list virtual machines: {exc}"
        )}
    if not vms:
        return {"ok": True, "step": "ready", "vms": 0, "message": (
            "Everything is in place. Hyper-V reports no virtual machines yet — create "
            "one, then open 'Hyper-V Console' from the Start Menu to choose it."
        )}
    return {"ok": True, "step": "ready", "vms": len(vms), "message": (
        "Everything is ready. Open 'Hyper-V Console' from the Start Menu; the first "
        "run asks which virtual machine to control."
    )}


# -- the routine ----------------------------------------------------------

def first_run(
    *,
    service: Any = None,
    environ: Mapping[str, str] | None = None,
    uvx: bool = False,
    profile: str | None = None,
    shortcut: bool = True,
    probe: Probe | None = None,
) -> dict[str, Any]:
    """The routine every install path ends in, in the order the operator meets it.

    A host that has to enable Hyper-V stops here with the command and comes
    back after the restart: this function never reboots a machine, and never
    asks for one. Everything else runs and reports, so a second run after a
    partial install is a repair rather than a repeat.
    """
    plan = assess(read_host(probe))
    steps: dict[str, Any] = {"host": plan}
    if plan["action"] == "stop":
        return {"ok": False, "stopped": "host", "message": plan["message"], "steps": steps}
    if plan["action"] == "enable-hyperv":
        return {
            "ok": False, "stopped": "hyper-v", "command": plan["command"],
            "message": plan["message"], "steps": steps,
        }
    steps["clients"] = configure_clients(
        uvx=uvx, profile=profile, environ=environ
    )
    if shortcut:
        steps["shortcut"] = install.create_shortcut(install.shortcut_path(environ))
    if service is not None:
        steps["self_test"] = self_test(service)
    ok = all(
        step.get("ok", True)
        for name, step in steps.items()
        if name in {"self_test"}
    )
    message = (
        steps["self_test"]["message"] if "self_test" in steps
        else "The install finished. Open 'Hyper-V Console' from the Start Menu."
    )
    return {"ok": ok, "stopped": None, "message": message, "steps": steps}


# -- uninstall ------------------------------------------------------------

def _drop_toml_section(text: str) -> str:
    """Remove our section and its ``.env`` sub-section, and nothing else."""
    kept: list[str] = []
    dropping = False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("["):
            dropping = stripped.startswith(f"[mcp_servers.{install.SERVER_NAME}")
        if not dropping:
            kept.append(line)
    return "".join(kept)


def remove_client_entry(
    client: str, *, environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Take our entry back out of one client's file, leaving the rest as it was.

    A file we cannot parse is left exactly as it is: an uninstall that guesses
    is worse than one that says it could not finish.
    """
    path = install.client_config(client, environ)
    if not path.is_file():
        return {"client": client, "path": str(path), "action": "absent"}
    text = path.read_text(encoding="utf-8")
    if client == "codex":
        remaining = _drop_toml_section(text)
        if remaining == text:
            return {"client": client, "path": str(path), "action": "unchanged"}
        return _rewrite(client, path, remaining)
    try:
        document = json.loads(text)
    except ValueError:
        return {"client": client, "path": str(path), "action": "refused",
                "reason": "the file is not valid JSON"}
    servers = document.get("mcpServers") if isinstance(document, dict) else None
    if not isinstance(servers, dict) or install.SERVER_NAME not in servers:
        return {"client": client, "path": str(path), "action": "unchanged"}
    del servers[install.SERVER_NAME]
    return _rewrite(client, path, json.dumps(document, indent=2, ensure_ascii=False) + "\n")


def _rewrite(client: str, path: Path, text: str) -> dict[str, Any]:
    backup = install.backup(path)
    install.write_atomic(path, text)
    return {
        "client": client,
        "path": str(path),
        "action": "removed",
        "backup": str(backup) if backup else None,
    }


def known_vms(environ: Mapping[str, str] | None = None) -> list[str]:
    """The VMs the operator's access file names, in the spelling it records.

    The spelling matters: the credential's target is built from the VM's name,
    so deleting ``lab-vm`` when the vault holds ``Lab-VM`` would leave the
    secret behind while reporting that it was removed.
    """
    store = AccessStore(access_file(environ))
    return sorted(name for name, _level in store.load().values())


def forget_credentials(vms: list[str]) -> list[dict[str, Any]]:
    """Delete this user's guest password and KDNET key for each VM."""
    keys = SecretStore(KDNET_TARGET_PREFIX)
    removed = []
    for vm in vms:
        removed.append({
            "vm": vm,
            "credential": CredentialStore().delete(vm),
            "kdnet_key": keys.delete(vm),
        })
    return removed


def uninstall(
    *,
    environ: Mapping[str, str] | None = None,
    clients: list[str] | None = None,
    credentials: bool = False,
    data: bool = False,
) -> dict[str, Any]:
    """Undo what the install wrote, and name what it deliberately left behind.

    Credentials and the state folder are the operator's own data — a guest
    password they may still need, the evidence of a run — so they go only when
    asked for, and are named with their location when they do not.
    """
    chosen = list(detect_clients(environ) if clients is None else clients)
    steps: dict[str, Any] = {
        "clients": [remove_client_entry(client, environ=environ) for client in chosen],
    }
    shortcut = install.shortcut_path(environ)
    if shortcut.is_file():
        shortcut.unlink()
        steps["shortcut"] = {"shortcut": str(shortcut), "action": "removed"}
    else:
        steps["shortcut"] = {"shortcut": str(shortcut), "action": "absent"}

    root = data_root(environ)
    if data:
        shutil.rmtree(root, ignore_errors=True)
        steps["data"] = {"path": str(root), "action": "removed"}
    else:
        steps["data"] = {"path": str(root), "action": "kept"}

    if credentials:
        steps["credentials"] = {
            "action": "removed", "vms": forget_credentials(known_vms(environ)),
        }
    else:
        steps["credentials"] = {
            "action": "kept",
            "where": "Credential Manager, under HyperVConsoleMCP/<vm> and HyperVConsoleMCP/kdnet/<vm>",
        }
    steps["message"] = (
        "The console is uninstalled. "
        + ("Its saved credentials and state folder were removed too."
           if credentials and data else
           "Saved credentials and the state folder were kept; run this with "
           "--remove-credentials and --remove-data to delete them.")
    )
    return steps


def _parser() -> argparse.ArgumentParser:
    """The command line, in one place so a test can check what it accepts."""
    parser = argparse.ArgumentParser(
        prog="hyperv-setup",
        description="Install Hyper-V Console MCP on this host, or say why it cannot.",
    )
    parser.add_argument("--check", action="store_true",
                        help="Report what this host needs and change nothing.")
    parser.add_argument("--uninstall", action="store_true",
                        help="Remove the client config, the shortcut and what was installed.")
    parser.add_argument("--remove-credentials", action="store_true",
                        help="With --uninstall: also delete this user's saved guest passwords and KDNET keys.")
    parser.add_argument("--remove-data", action="store_true",
                        help="With --uninstall: also delete the state, event and evidence folders.")
    parser.add_argument("--uvx", action="store_true",
                        help="Write `uvx hyperv-console-mcp` instead of this interpreter.")
    parser.add_argument("--profile", choices=("safe", "lab"))
    parser.add_argument("--no-shortcut", action="store_true")
    parser.add_argument("--no-self-test", action="store_true",
                        help="Skip the final check, which starts the service.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)

    if arguments.uninstall:
        result = uninstall(
            credentials=arguments.remove_credentials, data=arguments.remove_data,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(result["message"], file=sys.stderr)
        return 0

    if arguments.check:
        plan = assess(read_host())
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        print(plan["message"], file=sys.stderr)
        return 0 if plan["action"] == "proceed" else 1

    service = None
    backend = None
    if not arguments.no_self_test:
        from .config import Settings
        from .service.host import open_service

        service, backend = open_service(Settings.from_env(), actor="install")
    try:
        result = first_run(
            service=service, uvx=arguments.uvx, profile=arguments.profile,
            shortcut=not arguments.no_shortcut,
        )
    except HyperVError as exc:
        print(json.dumps({"ok": False, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if backend is not None:
            backend.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(result["message"], file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ENABLE_FEATURE",
    "HOME_EDITIONS",
    "Host",
    "assess",
    "configure_clients",
    "detect_clients",
    "first_run",
    "forget_credentials",
    "known_vms",
    "main",
    "read_host",
    "remove_client_entry",
    "self_test",
    "supports_hyperv",
    "uninstall",
]
