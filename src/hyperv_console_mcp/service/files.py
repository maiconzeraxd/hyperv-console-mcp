"""The host's own files, limited to the roots this server was configured with.

The guest side of the file manager reaches the whole lab machine, because that
machine exists to be broken. The host side does not: the window sees four
directories and nothing else, so a wrong click cannot walk into ``C:\\Windows``
or the operator's documents. Those four are the ones the rest of the server
already writes — a package to stage, evidence to collect, a transfer to land,
media to mount — and the rule lives here rather than in the page, because the
page is a client and the AI calls these same methods through the MCP tools.

Every method takes a root name and a path relative to it. An absolute path is
accepted only if it resolves inside that root, which is what keeps the
confinement true even when the caller builds the path itself.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import PolicyError, ValidationError
from ._base import HOST_ROOTS, ServiceBase, _text

# The four roots the window may browse, and the setting each one comes from.
# Order matters: it is the order the page draws them in.
ROOT_FIELDS: dict[str, str] = dict(HOST_ROOTS)

MAX_ENTRIES = 500


def _root(settings: Any, name: str) -> tuple[str, Path]:
    key = _text(name, "root", 32).strip().casefold()
    field = ROOT_FIELDS.get(key)
    if field is None:
        raise ValidationError(
            "root must be one of: " + ", ".join(ROOT_FIELDS) + "."
        )
    return key, Path(getattr(settings, field))


def _within(base: Path, relative: str, label: str = "path") -> Path:
    """Resolve a path against a root, refusing anything that leaves it.

    ``..`` is not banned by name: the resolved path is compared with the root,
    so a path that climbs out and a path that names an absolute location
    outside both fail the same check.
    """
    text = _text(relative, label, 32_768, allow_empty=True)
    try:
        root = base.resolve()
        target = Path(text)
        resolved = (target if target.is_absolute() else root / target).resolve()
    except OSError as exc:
        raise ValidationError(f"{label} could not be resolved: {exc}") from exc
    if resolved != root and not resolved.is_relative_to(root):
        raise PolicyError(f"{label} must remain under {root}")
    return resolved


def _relative(base: Path, path: Path) -> str:
    """A path as the page names it: relative to its root, empty for the root."""
    if path == base.resolve():
        return ""
    return str(path.relative_to(base.resolve()))


def _entry(base: Path, item: Path) -> dict[str, Any]:
    """One directory entry, in the shape the guest side answers with."""
    try:
        info = item.stat()
        is_directory = item.is_dir()
    except OSError as exc:
        return {
            "name": item.name, "path": _relative(base, item), "type": "unreadable",
            "size": None, "last_write_utc": None, "error": str(exc),
        }
    return {
        "name": item.name,
        "path": _relative(base, item),
        "type": "directory" if is_directory else "file",
        "size": None if is_directory else int(info.st_size),
        "last_write_utc": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
    }


class FileService(ServiceBase):
    def file_roots(self) -> dict[str, Any]:
        """The roots the file manager may browse, in the order it draws them.

        No capability is needed to read this: it names four directories and
        their existence, and a page that cannot show them cannot be used.
        """
        roots = []
        for name, field in ROOT_FIELDS.items():
            path = Path(getattr(self.settings, field))
            roots.append({"name": name, "path": str(path), "exists": path.is_dir()})
        return {"roots": roots}

    def host_list_dir(self, root: str, path: str = "") -> dict[str, Any]:
        """What one host directory holds, directories first, then files by name."""
        name, base = _root(self.settings, root)
        directory = _within(base, path)
        if not directory.is_dir():
            raise ValidationError(f"The host directory does not exist: {directory}")
        try:
            items = sorted(
                directory.iterdir(), key=lambda item: (item.is_file(), item.name.casefold())
            )
        except OSError as exc:
            raise ValidationError(f"The host directory could not be read: {exc}") from exc
        return {
            "root": name,
            "directory": {
                "path": _relative(base, directory),
                "entries": [_entry(base, item) for item in items[:MAX_ENTRIES]],
                "truncated": len(items) > MAX_ENTRIES,
            },
        }

    def host_make_dir(self, root: str, path: str) -> dict[str, Any]:
        """Create one directory; asking for one that exists is not an error."""
        name, base = _root(self.settings, root)
        target = _within(base, path)
        if not _relative(base, target):
            raise ValidationError("The root itself already exists.")
        relative = _relative(base, target)
        if target.is_dir():
            return {"root": name, "directory": {"path": relative, "created": False}}
        with self._approve("host_make_dir", None, {"root": name, "path": relative}):
            self._call("host_make_dir", None, lambda: target.mkdir(parents=True))
        return {"root": name, "directory": {"path": relative, "created": True}}

    def host_rename(self, root: str, path: str, new_name: str) -> dict[str, Any]:
        """Rename one file or directory inside its own parent directory."""
        name, base = _root(self.settings, root)
        source = _within(base, path)
        if not _relative(base, source):
            raise ValidationError("The root itself cannot be renamed.")
        target_name = _text(new_name, "new_name", 255)
        if any(character in target_name for character in '\\/:*?"<>|') or target_name in {".", ".."}:
            raise ValidationError("new_name must be a single file or directory name.")
        if not source.exists():
            raise ValidationError(f"The host path does not exist: {source}")
        target = source.parent / target_name
        _within(base, str(target), "new_name")
        if target.exists():
            raise ValidationError(f"The destination already exists: {target}")
        relative = _relative(base, target)
        with self._approve("host_rename", None, {"path": _relative(base, source), "new_name": target_name}):
            self._call("host_rename", None, lambda: source.rename(target))
        return {"root": name, "entry": {"path": relative, "previous_path": _relative(base, source)}}

    def host_delete(
        self,
        root: str,
        path: str,
        confirm_path: str,
        *,
        recursive: bool = False,
    ) -> dict[str, Any]:
        """Delete one file, or a directory the operator named twice.

        ``confirm_path`` is the relative path exactly as it was listed, which is
        what makes a stray click a refusal rather than a deletion.
        """
        name, base = _root(self.settings, root)
        target = _within(base, path)
        if not isinstance(recursive, bool):
            raise ValidationError("recursive must be a boolean value.")
        relative = _relative(base, target)
        if not relative:
            raise ValidationError("The root itself cannot be deleted.")
        self._confirm(confirm_path, relative, "confirm_path")
        if not target.exists():
            raise ValidationError(f"The host path does not exist: {target}")
        kind = "directory" if target.is_dir() else "file"
        with self._approve(
            "host_delete", None, {"root": name, "path": relative, "type": kind, "recursive": recursive}
        ):
            self._call("host_delete", None, lambda: self._remove(target, recursive))
        return {"root": name, "deleted": {"path": relative, "type": kind, "recursive": recursive}}

    @staticmethod
    def _remove(target: Path, recursive: bool) -> None:
        if target.is_dir():
            if recursive:
                shutil.rmtree(target)
            else:
                target.rmdir()
            return
        target.unlink()


__all__ = ["MAX_ENTRIES", "ROOT_FIELDS", "FileService"]
