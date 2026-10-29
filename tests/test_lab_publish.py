"""The host decides whether a staged package is what it approved.

Every case here is a package the guest could report and a verdict the host has
to reach on its own: the guest sends bytes and a listing, never a conclusion.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from hyperv_console_mcp.errors import ValidationError
from hyperv_console_mcp.lab.publish import (
    MAX_MANIFEST_BYTES,
    entry_path,
    validate_manifest,
    verify_staged,
)

PACKAGE = "lab-package-1"
DRIVER = b"MZ" + bytes(range(64))
README = b"# package\n"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_bytes(files: dict[str, bytes] | None = None, package_id: str = PACKAGE) -> bytes:
    files = {"driver.sys": DRIVER, "docs/readme.md": README} if files is None else files
    document = {
        "schema": 1,
        "package_id": package_id,
        "created_utc": "2026-10-29T00:00:00+00:00",
        "files": [
            {"path": path, "size": len(data), "sha256": digest(data)}
            for path, data in files.items()
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def listing(files: dict[str, bytes] | None = None, **extra: bytes) -> list[dict[str, object]]:
    files = {"driver.sys": DRIVER, "docs/readme.md": README} if files is None else files
    entries = [
        {"path": path, "size": len(data), "sha256": digest(data)}
        for path, data in files.items()
    ]
    entries.extend(
        {"path": path, "size": len(data), "sha256": digest(data)}
        for path, data in extra.items()
    )
    return entries


def verify(manifest: bytes | None = None, entries: list | None = None, **overrides):
    manifest = manifest_bytes() if manifest is None else manifest
    arguments = {
        "package_id": PACKAGE,
        "expected_sha256": digest(manifest),
        "manifest": manifest,
        "listing": listing() if entries is None else entries,
    }
    arguments.update(overrides)
    return verify_staged(**arguments)


def test_a_faithful_staging_directory_verifies() -> None:
    assert verify() == {"files_verified": 2, "manifest_sha256": digest(manifest_bytes())}


def test_the_manifest_is_hashed_from_the_bytes_the_guest_sent() -> None:
    """The guest's bytes are what gets hashed, not a re-serialization of them."""
    manifest = manifest_bytes()
    assert verify(manifest=manifest)["manifest_sha256"] == digest(manifest)
    # Same document, different bytes (indentation), so a different hash: the
    # approved digest is of the canonical bytes and must not match this one.
    reordered = json.dumps(json.loads(manifest), indent=2).encode("utf-8")
    with pytest.raises(ValidationError, match="host-approved hash"):
        verify_staged(
            package_id=PACKAGE,
            expected_sha256=digest(manifest),
            manifest=reordered,
            listing=listing(),
        )


def test_the_manifest_file_itself_is_not_counted_as_a_package_file() -> None:
    entries = listing(**{"lab-manifest.json": manifest_bytes()})
    assert verify(entries=entries)["files_verified"] == 2


def test_a_missing_file_is_named() -> None:
    entries = [entry for entry in listing() if entry["path"] != "docs/readme.md"]
    with pytest.raises(ValidationError, match="missing a file: docs/readme.md"):
        verify(entries=entries)


def test_a_changed_size_fails_verification() -> None:
    entries = listing()
    entries[0] = {**entries[0], "size": 1}
    with pytest.raises(ValidationError, match="failed verification: driver.sys"):
        verify(entries=entries)


def test_a_changed_hash_fails_verification() -> None:
    entries = listing()
    entries[1] = {**entries[1], "sha256": "0" * 64}
    with pytest.raises(ValidationError, match="failed verification: docs/readme.md"):
        verify(entries=entries)


def test_an_unlisted_file_is_refused_and_named() -> None:
    entries = listing(**{"leftover.sys": b"junk"})
    with pytest.raises(ValidationError, match="not in the manifest: leftover.sys"):
        verify(entries=entries)


def test_paths_compare_without_regard_to_case() -> None:
    """Windows would hand back DRIVER.SYS for driver.sys; the entry is the same."""
    entries = [{**entry, "path": str(entry["path"]).upper()} for entry in listing()]
    assert verify(entries=entries)["files_verified"] == 2


def test_a_manifest_for_another_package_is_refused() -> None:
    manifest = manifest_bytes(package_id="other-package")
    with pytest.raises(ValidationError, match="different package"):
        verify(manifest=manifest)


def test_an_expected_hash_that_is_not_a_digest_is_refused() -> None:
    with pytest.raises(ValidationError, match="lowercase SHA-256"):
        verify(expected_sha256="ABC")


def test_a_manifest_that_is_not_json_is_refused() -> None:
    manifest = b"not json at all"
    with pytest.raises(ValidationError, match="manifest is invalid"):
        verify(manifest=manifest)


def test_a_manifest_that_is_not_an_object_is_refused() -> None:
    manifest = b"[1, 2, 3]"
    with pytest.raises(ValidationError, match="manifest is invalid"):
        verify(manifest=manifest)


def test_an_oversized_manifest_is_refused() -> None:
    manifest = b" " * (MAX_MANIFEST_BYTES + 1)
    with pytest.raises(ValidationError, match="size limit"):
        verify(manifest=manifest)


def test_an_unsafe_path_in_the_manifest_is_refused() -> None:
    document = json.loads(manifest_bytes())
    document["files"][0]["path"] = "../escape.sys"
    manifest = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with pytest.raises(ValidationError, match="unsafe or non-portable"):
        verify(manifest=manifest)


@pytest.mark.parametrize(
    "manifest",
    [
        {"schema": 2, "package_id": PACKAGE, "files": [{"path": "a", "size": 1, "sha256": "0" * 64}]},
        {"schema": 1, "package_id": PACKAGE, "files": []},
        {"schema": 1, "package_id": "", "files": [{"path": "a", "size": 1, "sha256": "0" * 64}]},
        {"schema": 1, "package_id": PACKAGE, "files": [{"path": "a", "size": -1, "sha256": "0" * 64}]},
    ],
)
def test_an_invalid_manifest_is_refused(manifest: dict) -> None:
    with pytest.raises(ValidationError):
        validate_manifest(manifest)


def test_a_case_insensitive_duplicate_is_refused() -> None:
    entry = {"size": 1, "sha256": "0" * 64}
    with pytest.raises(ValidationError, match="duplicate"):
        validate_manifest({
            "schema": 1,
            "package_id": PACKAGE,
            "files": [{"path": "driver.sys", **entry}, {"path": "DRIVER.SYS", **entry}],
        })


@pytest.mark.parametrize(
    "name", ["../escape.sys", "café.sys", "bad?.sys", "NUL", "folder.", "my file.sys", "a//b.sys"]
)
def test_a_nonportable_path_is_refused(name: str) -> None:
    with pytest.raises(ValidationError):
        entry_path({"path": name})


@pytest.mark.parametrize("name", ["driver.sys", "docs/readme.md", "a-b_c.d/e.f"])
def test_a_portable_path_is_kept_as_written(name: str) -> None:
    assert entry_path({"path": name}) == name
