"""Guest credentials in the Windows Credential Manager, never in tool arguments.

The password lives under the generic target ``HyperVConsoleMCP/<vm>`` in the
current user's vault (DPAPI-protected, readable only by that user). The
environment variables remain a fallback for existing setups, but the store is
the primary path and wins when both exist.
"""

from __future__ import annotations

import ctypes
import secrets
import sys
from ctypes import wintypes
from typing import Any

from .errors import BackendError, PolicyError

TARGET_PREFIX = "HyperVConsoleMCP/"
# A KDNET key is not a credential pair, but it is a secret with the same rules:
# it never appears in a tool argument, a result, an event, or the audit log.
KDNET_TARGET_PREFIX = "HyperVConsoleMCP/kdnet/"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


def target_name(vm_name: str) -> str:
    return TARGET_PREFIX + vm_name


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _CredentialAttribute(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", ctypes.POINTER(ctypes.c_byte)),
    ]


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FileTime),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.POINTER(_CredentialAttribute)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _advapi32() -> Any:
    if sys.platform != "win32":
        raise PolicyError(
            "The Windows Credential Manager is only available on Windows. "
            "Set HYPERV_MCP_GUEST_PASSWORD in the protected server environment instead."
        )
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredReadW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_Credential)),
    ]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def _read(target: str) -> tuple[str, str] | None:
    """Return (username, password) for a target, or None when it is absent."""
    api = _advapi32()
    pointer = ctypes.POINTER(_Credential)()
    if not api.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        code = ctypes.get_last_error()
        if code == ERROR_NOT_FOUND:
            return None
        raise BackendError(
            f"Reading the stored guest credential failed (Windows error {code}).",
            code="credential_store_failed",
        )
    try:
        credential = pointer.contents
        size = int(credential.CredentialBlobSize)
        blob = ctypes.string_at(credential.CredentialBlob, size) if size else b""
        return (str(credential.UserName or ""), blob.decode("utf-16-le", "replace"))
    finally:
        api.CredFree(pointer)


def _write(target: str, username: str, password: str) -> None:
    api = _advapi32()
    blob = password.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(blob, len(blob))
    credential = _Credential()
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.UserName = username
    credential.Comment = "hyperv-console-mcp guest credential"
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    if not api.CredWriteW(ctypes.byref(credential), 0):
        code = ctypes.get_last_error()
        raise BackendError(
            f"Storing the guest credential failed (Windows error {code}).",
            code="credential_store_failed",
        )


def _delete(target: str) -> bool:
    api = _advapi32()
    if api.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
        return True
    code = ctypes.get_last_error()
    if code == ERROR_NOT_FOUND:
        return False
    raise BackendError(
        f"Deleting the stored guest credential failed (Windows error {code}).",
        code="credential_store_failed",
    )


class CredentialStore:
    """The current user's Credential Manager, one generic credential per VM."""

    def read(self, vm_name: str) -> tuple[str, str] | None:
        return _read(target_name(vm_name))

    def password(self, vm_name: str) -> str | None:
        entry = self.read(vm_name)
        return entry[1] if entry and entry[1] else None

    def write(self, vm_name: str, username: str, password: str) -> None:
        if not username or not password:
            raise PolicyError("A guest credential needs both a username and a password.")
        _write(target_name(vm_name), username, password)

    def delete(self, vm_name: str) -> bool:
        return _delete(target_name(vm_name))

    def has(self, vm_name: str) -> bool:
        return self.read(vm_name) is not None


class SecretStore:
    """Generic secrets in the current user's vault under a caller-chosen prefix.

    The username field carries the key name, so a KDNET key for a VM reads as
    target ``HyperVConsoleMCP/kdnet/<vm>`` with user ``kdnet``.
    """

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def _target(self, key: str) -> str:
        return self.prefix + key

    def read(self, key: str) -> str | None:
        entry = _read(self._target(key))
        return entry[1] if entry and entry[1] else None

    def write(self, key: str, secret: str, *, label: str = "kdnet") -> None:
        if not secret:
            raise PolicyError("A stored secret cannot be empty.")
        _write(self._target(key), label, secret)

    def delete(self, key: str) -> bool:
        return _delete(self._target(key))

    def has(self, key: str) -> bool:
        try:
            return _read(self._target(key)) is not None
        except PolicyError:
            return False


def resolve(settings: Any, vm_name: str) -> tuple[str, str]:
    """Username and password for PowerShell Direct, store first, env as fallback.

    The fallback is reported by the caller, never silently: an environment
    variable leaks into child processes and crash dumps in ways a vault entry
    does not.
    """
    stored = CredentialStore().read(vm_name)
    if stored and stored[1]:
        return stored[0] or (settings.guest_username or ""), stored[1]
    username = settings.guest_username or ""
    password = settings.guest_password or ""
    if not username or not password:
        raise PolicyError(
            f"No guest credential for {vm_name}. Set one with guest_credential_set "
            "or provide HYPERV_MCP_GUEST_USERNAME and HYPERV_MCP_GUEST_PASSWORD."
        )
    return username, password


def source(settings: Any, vm_name: str) -> str:
    """Where the credential came from, for status output. Never returns the secret."""
    try:
        if CredentialStore().has(vm_name):
            return "credential_manager"
    except PolicyError:
        pass
    if settings.guest_username and settings.guest_password:
        return "environment"
    return "missing"


PASSWORD_LENGTH = 24
# Characters that survive PowerShell, cmd, sshd and the Windows logon dialog
# without quoting tricks: no quotes, backtick, dollar, semicolon or space.
_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_LOWER = "abcdefghijkmnopqrstuvwxyz"
_DIGITS = "23456789"
_SYMBOLS = "!#%+-=?@_"


def generate_password(length: int = PASSWORD_LENGTH) -> str:
    """A random password that satisfies a default Windows complexity policy."""
    if length < 16:
        raise PolicyError("A generated guest password must be at least 16 characters.")
    alphabet = _UPPER + _LOWER + _DIGITS + _SYMBOLS
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(character in _UPPER for character in candidate)
            and any(character in _LOWER for character in candidate)
            and any(character in _DIGITS for character in candidate)
            and any(character in _SYMBOLS for character in candidate)
        ):
            return candidate


__all__ = [
    "CRED_TYPE_GENERIC",
    "KDNET_TARGET_PREFIX",
    "PASSWORD_LENGTH",
    "TARGET_PREFIX",
    "CredentialStore",
    "SecretStore",
    "generate_password",
    "resolve",
    "source",
    "target_name",
]
