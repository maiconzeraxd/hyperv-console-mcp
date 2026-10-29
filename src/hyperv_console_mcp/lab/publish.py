"""What a staged package must look like before it may be published.

The guest's job is to report; the decision is made here. A guest that both
hashed and compared would have to agree with this module about canonical JSON,
path spelling and case, and every divergence would surface as a bare "failed
verification" from inside the VM. So the guest returns the manifest bytes and a
plain listing of what it actually has, and this module compares them with the
manifest the host approved.

Nothing here touches a VM: ``verify_staged`` is a pure function of three values
the transport collected, which is what makes the whole class of guest-side
canonical-hash bugs testable from Python.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any

from ..errors import ValidationError

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024
MAX_PACKAGE_FILES = 512
MANIFEST_NAME = "lab-manifest.json"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}")
# Package paths must stay portable ASCII so every serializer involved agrees
# byte for byte.
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
})
_SHA256 = re.compile(r"[0-9a-f]{64}")


def identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValidationError(f"{label} must be 1-64 letters, digits, dots, or hyphens.")
    return value


def entry_path(entry: dict[str, Any]) -> str:
    """The relative path a manifest or an evidence index names, or a refusal."""
    relative = PurePosixPath(entry.get("path", ""))
    text = relative.as_posix()
    if (
        not relative.parts or relative.is_absolute() or ".." in relative.parts
        or text != entry.get("path") or len(text) > 240
    ):
        raise ValidationError("The manifest contains an unsafe or non-portable relative path.")
    for part in relative.parts:
        if (
            part in {".", ".."} or _PATH_SEGMENT.fullmatch(part) is None
            or part.endswith(".")
            or part.split(".", 1)[0].upper() in _RESERVED_NAMES
        ):
            raise ValidationError("The manifest contains an unsafe or non-portable relative path.")
    return text


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), list):
        raise ValidationError("Unsupported or incomplete lab manifest.")
    identifier(manifest.get("package_id", ""), "package_id")
    if not 1 <= len(manifest["files"]) <= MAX_PACKAGE_FILES:
        raise ValidationError("The manifest file count is outside its limits.")
    paths = set()
    total = 0
    for entry in manifest["files"]:
        path = entry_path(entry)
        size = entry.get("size")
        if (
            not isinstance(size, int) or isinstance(size, bool)
            or path.casefold() in paths or size < 0 or len(entry.get("sha256", "")) != 64
        ):
            raise ValidationError("The manifest contains a duplicate or invalid file entry.")
        if not _SHA256.fullmatch(entry["sha256"]):
            raise ValidationError("The manifest contains an invalid SHA-256.")
        paths.add(path.casefold())
        total += entry["size"]
    if total > MAX_PACKAGE_BYTES:
        raise ValidationError("The manifest exceeds the 2 GiB package limit.")


def verify_staged(
    *,
    package_id: str,
    expected_sha256: str,
    manifest: bytes,
    listing: list[dict[str, Any]],
    label: str = "staged",
) -> dict[str, Any]:
    """Check what the guest says it has against the manifest the host approved.

    ``manifest`` is the bytes the guest found on disk, not the ones the host
    sent: the point is to prove what landed is what was approved, so the hash is
    computed here, from those bytes, and compared with the approved one.
    ``listing`` is the guest's own inventory of the directory — every file, with
    its size and SHA-256 — and it is compared entry by entry. ``label`` names the
    directory in a refusal: a staged package and a published one are checked the
    same way, and the message should say which one failed.
    """
    identifier(package_id, "package_id")
    if not _SHA256.fullmatch(expected_sha256 or ""):
        raise ValidationError("manifest_sha256 must be a lowercase SHA-256 hexadecimal string.")
    if len(manifest) > MAX_MANIFEST_BYTES:
        raise ValidationError("The staged manifest exceeds its size limit.")
    actual = hashlib.sha256(manifest).hexdigest()
    if actual != expected_sha256:
        raise ValidationError("The staged manifest does not match the host-approved hash.")
    try:
        document = json.loads(manifest.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("The staged manifest is invalid.") from exc
    if not isinstance(document, dict):
        raise ValidationError("The staged manifest is invalid.")
    validate_manifest(document)
    if document["package_id"] != package_id:
        raise ValidationError("The staged manifest is for a different package.")

    present: dict[str, dict[str, Any]] = {}
    for entry in listing:
        path = str(entry.get("path") or "")
        if not path or path == MANIFEST_NAME:
            continue
        present[path.casefold()] = entry

    verified = 0
    for entry in document["files"]:
        relative = entry_path(entry)
        found = present.pop(relative.casefold(), None)
        if found is None:
            raise ValidationError(f"The {label} package is missing a file: {relative}")
        if (
            _int(found.get("size")) != entry["size"]
            or str(found.get("sha256") or "").casefold() != entry["sha256"]
        ):
            raise ValidationError(f"The {label} package failed verification: {relative}")
        verified += 1
    if present:
        extra = ", ".join(sorted(entry["path"] for entry in present.values()))
        raise ValidationError(
            f"The {label} package contains files that are not in the manifest: {extra}"
        )
    return {"files_verified": verified, "manifest_sha256": expected_sha256}


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1
