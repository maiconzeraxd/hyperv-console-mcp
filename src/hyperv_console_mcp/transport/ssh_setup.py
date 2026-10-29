"""Host-side SSH provisioning: keys, pinned host key, state, and the sequence.

Provisioning always talks to the guest over PowerShell Direct, because SSH does
not exist in the guest until this code creates it. The host side only touches
the dedicated internal switch and that VM's adapter; nothing here can attach an
external switch.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import BackendError, PolicyError, ValidationError
from .psdirect import PowerShellDirectTransport
from .ssh import ERROR_MARKER, JSON_MARKER, SshTransport, ps_quote, vm_key

SWITCH_NAME = "HyperV-MCP-Lab"
ADAPTER_NAME = "HyperV-MCP-Lab"
GUEST_SCRIPT = r"C:\ProgramData\HyperVConsoleMCP\guest_ssh_setup.ps1"
GUEST_OPENSSH_ROOT = r"C:\ProgramData\HyperVConsoleMCP\openssh"
HEALTH_ATTEMPTS = 20
HEALTH_DELAY_SECONDS = 0.5
MAX_PACKAGE_FILES = 200
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
PACKAGE_MARKER = "HVMCP_NEEDS_PACKAGE:"

# The guest may have no internet, so `Add-WindowsCapability` cannot be relied on.
# This is the pinned Win32-OpenSSH release the host falls back to: the version,
# the size and the hash are all fixed here, and a download that does not match is
# refused rather than installed. Bump all three together, from a release whose
# hash was computed from the downloaded asset.
OPENSSH_RELEASE = {
    "version": "10.4.0.0p1-Preview",
    "asset": "OpenSSH-Win64.zip",
    "size": 6021393,
    "sha256": "b67ff8becd203502190ec42ac7787c24c2cd3325ed7700485fe7a1c83d2a7957",
}
OPENSSH_URL = (
    "https://github.com/PowerShell/Win32-OpenSSH/releases/download/"
    f"{OPENSSH_RELEASE['version']}/{OPENSSH_RELEASE['asset']}"
)


# -- addresses and state ----------------------------------------------------


def host_address(settings: Any) -> tuple[ipaddress.IPv4Network, str]:
    try:
        network = ipaddress.ip_network(str(settings.ssh_subnet), strict=False)
    except ValueError as exc:
        raise ValidationError(f"HYPERV_MCP_SSH_SUBNET is not a network: {exc}") from exc
    if network.version != 4 or network.prefixlen > 30 or network.num_addresses < 4:
        raise ValidationError("HYPERV_MCP_SSH_SUBNET must be an IPv4 network with room for two hosts.")
    return network, str(network.network_address + 1)


def guest_address(settings: Any, vm: str) -> str:
    """A stable address per VM inside the lab subnet, never the host's."""
    network, _ = host_address(settings)
    digest = hashlib.sha256(vm.casefold().encode("utf-8")).digest()
    usable = network.num_addresses - 3  # network, host and broadcast are out
    offset = 2 + (int.from_bytes(digest[:4], "big") % max(usable - 1, 1))
    return str(network.network_address + offset)


def state_directory(settings: Any, vm: str) -> Path:
    return Path(settings.ssh_root) / vm_key(vm)


def read_state(settings: Any, vm: str) -> dict[str, Any] | None:
    path = state_directory(settings, vm) / "state.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_state(settings: Any, vm: str, state: dict[str, Any]) -> Path:
    directory = state_directory(settings, vm)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "state.json"
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    _restrict_acl(directory)
    return path


def _restrict_acl(path: Path) -> None:
    """Keep the key directory to the current user on Windows.

    The grant has to land before inheritance is cut: removing inherited ACEs
    first empties the DACL of files already inside the directory, which locks
    the owner out of its own key. Granting recursively and only then dropping
    inheritance leaves every child with the current user alone.
    """
    if sys.platform != "win32":
        return
    user = os.environ.get("USERNAME") or ""
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/grant:r", f"{user}:(OI)(CI)F", "/T", "/C"],
            check=False, capture_output=True, timeout=15,
        )
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/C"],
            check=False, capture_output=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return


# -- keys -------------------------------------------------------------------


def ensure_keypair(directory: Path) -> str:
    """Create (once) an ed25519 key pair and return the public key line."""
    directory.mkdir(parents=True, exist_ok=True)
    private = directory / "id_ed25519"
    public = directory / "id_ed25519.pub"
    if private.is_file() and public.is_file():
        return public.read_text(encoding="utf-8").strip()
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:  # pragma: no cover - the ssh extra provides it
        raise PolicyError(
            "Generating an SSH key needs cryptography. Install "
            "python -m pip install 'hyperv-console-mcp[ssh]'."
        ) from exc
    key = Ed25519PrivateKey.generate()
    private_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    private.write_bytes(private_bytes)
    public.write_text(public_bytes.decode("ascii") + "\n", encoding="utf-8")
    _restrict_acl(directory)
    return public_bytes.decode("ascii")


# -- markers ----------------------------------------------------------------


def _marker_result(output: str) -> dict[str, Any]:
    for line in output.splitlines():
        if line.startswith(PACKAGE_MARKER):
            raise BackendError(
                "The guest has no OpenSSH Server and could not install the capability: "
                f"{line[len(PACKAGE_MARKER):]}",
                code="openssh_package_required",
            )
        if line.startswith(JSON_MARKER):
            payload = line[len(JSON_MARKER):].strip()
            try:
                return json.loads(base64.b64decode(payload).decode("utf-8"))
            except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
                # A result clipped by the output limit used to surface here as a
                # bare "Invalid base64-encoded string", which says nothing about
                # the guest. Name the real cause instead.
                raise BackendError(
                    "The guest SSH setup result could not be decoded "
                    f"({exc}); it was probably cut by the output limit.",
                    code="ssh_setup_failed",
                ) from exc
        if line.startswith(ERROR_MARKER):
            raise BackendError(
                f"The guest SSH setup failed: {line[len(ERROR_MARKER):]}", code="ssh_setup_failed"
            )
    raise BackendError(
        f"The guest SSH setup produced no result: {output.strip()[:400]}", code="ssh_setup_failed"
    )


# -- offline package --------------------------------------------------------


def package_directory(settings: Any) -> Path:
    return Path(settings.ssh_root) / "_packages" / OPENSSH_RELEASE["version"]


def ensure_package(settings: Any) -> Path:
    """Return an extracted, hash-verified Win32-OpenSSH tree, downloading it once.

    The guest is disposable and often has no internet, so the host carries the
    package. The download is verified against the pinned size and SHA-256 before
    anything is extracted; a mismatch is refused, never installed.
    """
    root = package_directory(settings)
    installer = root / "install-sshd.ps1"
    if installer.is_file():
        return root
    root.parent.mkdir(parents=True, exist_ok=True)
    archive = root.parent / OPENSSH_RELEASE["asset"]
    if not archive.is_file():
        _download(OPENSSH_URL, archive)
    data = archive.read_bytes()
    if len(data) != OPENSSH_RELEASE["size"]:
        raise BackendError(
            f"The OpenSSH package has {len(data)} bytes; {OPENSSH_RELEASE['size']} were expected.",
            code="openssh_package_invalid",
        )
    digest = hashlib.sha256(data).hexdigest()
    if digest != OPENSSH_RELEASE["sha256"]:
        raise BackendError(
            "The OpenSSH package does not match the pinned SHA-256; refusing to install it.",
            code="openssh_package_invalid",
        )
    _extract(archive, root)
    if not installer.is_file():
        raise BackendError(
            f"The OpenSSH package has no install-sshd.ps1 after extraction: {root}",
            code="openssh_package_invalid",
        )
    return root


def _download(url: str, target: Path) -> None:
    import urllib.request

    partial = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        partial.replace(target)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise BackendError(
            f"Could not download the OpenSSH package from {url}: {exc}", code="openssh_download_failed"
        ) from exc


def _extract(archive: Path, root: Path) -> None:
    """Extract the archive, refusing entries that would escape ``root``."""
    import zipfile

    staging = root.with_name(root.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                name = member.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    raise BackendError(
                        f"The OpenSSH package contains an unsafe path: {member.filename}",
                        code="openssh_package_invalid",
                    )
                bundle.extract(member, staging)
    except zipfile.BadZipFile as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise BackendError(
            f"The OpenSSH package is not a readable zip: {exc}", code="openssh_package_invalid"
        ) from exc
    # The release ships one top-level directory; the installer lives inside it.
    entries = [path for path in staging.iterdir() if path.is_dir()]
    source = entries[0] if len(entries) == 1 and len(list(staging.iterdir())) == 1 else staging
    shutil.rmtree(root, ignore_errors=True)
    source.replace(root)
    shutil.rmtree(staging, ignore_errors=True)


# -- provisioning steps -----------------------------------------------------


def _adapter_mac(service: Any, vm: str, adapter_name: str) -> str:
    result = service._run("vm_get", "vm_get", {"vm": vm}, vm_name=vm, audit=False)
    # `vm_get` reports the adapters as `network_adapters`; reading a key that does
    # not exist made every provisioning attempt fail on a healthy adapter.
    adapters = result.get("vm", {}).get("network_adapters") or []
    for adapter in adapters:
        if str(adapter.get("name")) == adapter_name and adapter.get("mac_address"):
            return str(adapter["mac_address"])
    raise BackendError(
        f"The lab adapter {adapter_name!r} on {vm} has no MAC address; is the VM running?",
        code="ssh_setup_failed",
    )


def _upload_openssh(transport: PowerShellDirectTransport, vm: str, directory: Path) -> str:
    """Copy an extracted Win32-OpenSSH release into the guest and return its path."""
    root = Path(directory).expanduser()
    if not root.is_dir():
        raise ValidationError(f"HYPERV_MCP_OPENSSH_PACKAGE is not a directory: {root}")
    files = [path for path in sorted(root.rglob("*")) if path.is_file()]
    if not files:
        raise ValidationError(f"HYPERV_MCP_OPENSSH_PACKAGE is empty: {root}")
    total = sum(path.stat().st_size for path in files)
    if len(files) > MAX_PACKAGE_FILES or total > MAX_PACKAGE_BYTES:
        raise ValidationError(
            "HYPERV_MCP_OPENSSH_PACKAGE is larger than this tool will copy "
            f"({len(files)} files, {total} bytes)."
        )
    for path in files:
        relative = path.relative_to(root).as_posix().replace("/", "\\")
        target = f"{GUEST_OPENSSH_ROOT}\\{relative}"
        transport.put_file(vm, path, target, overwrite=True)
    return GUEST_OPENSSH_ROOT


def _install_command(
    public_key: str, host_ip: str, guest_ip: str, prefix: int, mac: str, sshd_source: str
) -> str:
    parts = [
        "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass",
        f"-File {ps_quote(GUEST_SCRIPT)}",
        "-Mode Install",
        f"-PublicKey {ps_quote(public_key)}",
        f"-HostIp {ps_quote(host_ip)}",
        f"-GuestIp {ps_quote(guest_ip)}",
        f"-PrefixLength {int(prefix)}",
        f"-MacAddress {ps_quote(mac)}",
    ]
    if sshd_source:
        parts.append(f"-SshdSource {ps_quote(sshd_source)}")
    return " ".join(parts)


def _upload_script(transport: PowerShellDirectTransport, vm: str) -> None:
    """Put the guest-side setup script where the command line expects it.

    Every mode runs the script by path, so every mode has to ship it first: a
    guest that was never provisioned has no copy, and `status` on such a guest
    used to fail with "the argument ... does not exist" instead of reporting
    that SSH is simply not set up.
    """
    source = Path(__file__).with_name("guest_ssh_setup.ps1")
    transport.put_file(vm, source, GUEST_SCRIPT, overwrite=True)


def _probe(settings: Any, vm: str) -> dict[str, Any]:
    """Wait for sshd to answer; a failure is reported, never hidden."""
    transport = SshTransport(settings)
    last: str | None = None
    for _ in range(HEALTH_ATTEMPTS):
        try:
            transport.health(vm)
            return {"ok": True, "error": None}
        except Exception as exc:  # paramiko raises many types; all mean "not yet"
            last = str(exc)
            time.sleep(HEALTH_DELAY_SECONDS)
    return {"ok": False, "error": last}


def _install(
    service: Any, transport: PowerShellDirectTransport, vm: str, *,
    public_key: str, host_ip: str, guest_ip: str, prefix: int, mac: str,
    sshd_source: str,
) -> dict[str, Any]:
    settings = service.settings
    execution = transport.exec(
        vm,
        _install_command(public_key, host_ip, guest_ip, prefix, mac, sshd_source),
        timeout=settings.timeout_seconds,
        max_output=settings.max_output_chars,
    )
    return _marker_result(execution.output)


def _undo_host_changes(service: Any, vm: str, prepared: dict[str, Any], host_ip: str) -> list[str]:
    """Put the host back after a provisioning attempt that did not finish.

    The network is prepared before the guest work, so a guest that refuses would
    otherwise leave the VM moved onto the lab switch — and a later deprovision
    would disconnect it, taking the guest's own network away with it.
    """
    try:
        removed = service._run(
            "guest_ssh_provision", "ssh_network_remove",
            {
                "vm": vm, "switch_name": SWITCH_NAME,
                "adapter_name": str(prepared.get("adapter_name") or ADAPTER_NAME),
                "host_ip": host_ip,
                "remove_adapter": bool(prepared.get("adapter_created")),
                "remove_host_address": bool(prepared.get("host_address_created")),
                "previous_switch": str(prepared.get("previous_switch") or ""),
            },
            vm_name=vm,
        )
    except Exception:  # noqa: BLE001 - the original failure is what matters
        return []
    return [str(item) for item in removed.get("removed") or []]


def provision(service: Any, vm: str, *, use_existing_adapter: bool = False) -> dict[str, Any]:
    settings = service.settings
    network, host_ip = host_address(settings)
    guest_ip = guest_address(settings, vm)
    transport = PowerShellDirectTransport(service.backend, settings)
    transport.health(vm)  # Fail before touching the host if the guest is unreachable.

    prepared = service._run(
        "guest_ssh_provision", "ssh_network_prepare",
        {
            "vm": vm, "switch_name": SWITCH_NAME, "host_ip": host_ip,
            "prefix_length": network.prefixlen, "adapter_name": ADAPTER_NAME,
            "use_existing_adapter": use_existing_adapter,
        },
        vm_name=vm,
    )
    try:
        mac = _adapter_mac(service, vm, str(prepared["adapter_name"]))

        directory = state_directory(settings, vm)
        public_key = ensure_keypair(directory)
        _upload_script(transport, vm)
        sshd_source = ""
        if settings.openssh_package:
            sshd_source = _upload_openssh(transport, vm, Path(settings.openssh_package))

        try:
            guest = _install(
                service, transport, vm,
                public_key=public_key, host_ip=host_ip, guest_ip=guest_ip,
                prefix=network.prefixlen, mac=mac, sshd_source=sshd_source,
            )
        except BackendError as exc:
            if exc.code != "openssh_package_required" or sshd_source:
                raise
            # The guest has no OpenSSH and no internet to fetch the capability.
            # Ship the pinned package from the host and try once more.
            sshd_source = _upload_openssh(transport, vm, ensure_package(settings))
            guest = _install(
                service, transport, vm,
                public_key=public_key, host_ip=host_ip, guest_ip=guest_ip,
                prefix=network.prefixlen, mac=mac, sshd_source=sshd_source,
            )
    except BaseException:
        _undo_host_changes(service, vm, prepared, host_ip)
        raise

    host_key = str(guest.get("host_key_public") or "").strip()
    if not host_key.startswith("ssh-ed25519 "):
        _undo_host_changes(service, vm, prepared, host_ip)
        raise BackendError("The guest did not report an ed25519 host key.", code="ssh_setup_failed")

    (directory / "known_hosts").write_text(f"{guest_ip} {host_key}\n", encoding="utf-8")
    state = {
        "vm": vm,
        "host": guest_ip,
        "port": 22,
        "switch_name": SWITCH_NAME,
        "adapter_name": prepared["adapter_name"],
        "adapter_created": bool(prepared["adapter_created"]),
        # Empty when the adapter was created for this lab or was already on the
        # lab switch; otherwise the switch provisioning moved it off.
        "previous_switch": str(prepared.get("previous_switch") or ""),
        "host_ip": host_ip,
        "host_address_created": bool(prepared["host_address_created"]),
        "provisioned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_state(settings, vm, state)

    probe = _probe(settings, vm)
    if not probe["ok"]:
        # Do not leave a state file that would make `auto` prefer a dead transport,
        # and do not leave the host moved onto a switch the guest never used.
        (directory / "state.json").unlink(missing_ok=True)
        _undo_host_changes(service, vm, prepared, host_ip)
        raise BackendError(
            "SSH was configured in the guest but never answered: "
            f"{probe['error']}. Guest state: {guest.get('state')}",
            code="ssh_unavailable",
        )
    return {
        "vm": vm, "transport": "ssh", "host": guest_ip, "port": 22,
        "switch_name": SWITCH_NAME, "adapter_name": state["adapter_name"],
        "adapter_created": state["adapter_created"],
        "host_address_created": state["host_address_created"],
        "sshd_source": guest.get("sshd_source"),
        "verified": True,
        "guest": guest.get("state"),
    }


def status(service: Any, vm: str) -> dict[str, Any]:
    settings = service.settings
    state = read_state(settings, vm)
    transport = PowerShellDirectTransport(service.backend, settings)
    guest: dict[str, Any]
    try:
        _upload_script(transport, vm)
        command = (
            "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass "
            f"-File {ps_quote(GUEST_SCRIPT)} -Mode Status"
        )
        execution = transport.exec(
            vm, command, timeout=settings.timeout_seconds, max_output=settings.max_output_chars
        )
        guest = _marker_result(execution.output)
    except Exception as exc:
        guest = {"reachable": False, "error": str(exc)}
    health: dict[str, Any] | None = None
    if state is not None:
        try:
            health = SshTransport(settings).health(vm)
        except Exception as exc:
            health = {"ok": False, "error": str(exc)}
    return {
        "vm": vm,
        "provisioned": state is not None,
        "state": state,
        "guest": guest,
        "ssh_health": health,
    }


def deprovision(service: Any, vm: str) -> dict[str, Any]:
    settings = service.settings
    state = read_state(settings, vm) or {}
    transport = PowerShellDirectTransport(service.backend, settings)
    guest: dict[str, Any] = {"removed": False}
    guest_ip = str(state.get("host") or guest_address(settings, vm))
    try:
        _upload_script(transport, vm)
        command = (
            "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass "
            f"-File {ps_quote(GUEST_SCRIPT)} -Mode Remove -GuestIp {ps_quote(guest_ip)}"
        )
        mac = state.get("adapter_name")
        if mac:
            command += f" -MacAddress {ps_quote(_adapter_mac(service, vm, str(mac)))}"
        execution = transport.exec(
            vm, command, timeout=settings.timeout_seconds, max_output=settings.max_output_chars
        )
        guest = _marker_result(execution.output)
    except Exception as exc:
        guest = {"removed": False, "error": str(exc)}

    host = service._run(
        "guest_ssh_deprovision", "ssh_network_remove",
        {
            "vm": vm, "switch_name": str(state.get("switch_name") or SWITCH_NAME),
            "adapter_name": str(state.get("adapter_name") or ADAPTER_NAME),
            "host_ip": str(state.get("host_ip") or host_address(settings)[1]),
            "remove_adapter": bool(state.get("adapter_created")),
            "remove_host_address": bool(state.get("host_address_created")),
            "previous_switch": str(state.get("previous_switch") or ""),
        },
        vm_name=vm,
    )
    try:
        SshTransport(settings).close(vm)
    except Exception:
        pass
    directory = state_directory(settings, vm)
    removed_state = (directory / "state.json").is_file()
    for name in ("state.json", "known_hosts"):
        (directory / name).unlink(missing_ok=True)
    return {
        "vm": vm, "transport": "ssh", "guest": guest, "host": host,
        "state_removed": removed_state,
    }


__all__ = [
    "ADAPTER_NAME", "GUEST_SCRIPT", "OPENSSH_RELEASE", "OPENSSH_URL", "SWITCH_NAME",
    "deprovision", "ensure_keypair", "ensure_package", "guest_address", "host_address",
    "package_directory", "provision", "read_state", "state_directory", "status", "write_state",
]
