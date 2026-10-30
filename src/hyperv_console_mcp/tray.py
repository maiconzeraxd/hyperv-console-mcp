"""``hyperv-tray``: the window's door in the notification area.

The tray is a client of the shared service and nothing else. It puts the window
one click away, says on hover whether the host is answering, and exits. It
holds no state and enforces no rule of its own: the window it opens is the same
program the Start Menu shortcut opens, and the status it shows is the answer
the service already gives the AI.

The icon is optional. Without ``pystray`` the command says what to install and
exits, and nothing else in the package imports it.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Mapping

from . import frozen
from .errors import HyperVError

TITLE = "Hyper-V Console"
OPEN = "Open"
STATUS = "Status"
QUIT = "Quit"
MENU_LABELS = (OPEN, STATUS, QUIT)

# A tooltip is read at a glance; asking the host more often than this would be
# polling for the sake of it, and the window shows the live view anyway.
REFRESH_SECONDS = 20.0

# The window this tray started, so a second click does not open a second one.
_window: Any = None


def status_text(host: Mapping[str, Any], fleet: Mapping[str, Any]) -> str:
    """One line for the tooltip: how much of the fleet is up, and what is allowed.

    Counts and the profile only. A tooltip sits on a shared desktop, so no VM
    name goes on it.
    """
    vms = list(fleet.get("vms") or ())
    running = sum(1 for vm in vms if str(vm.get("state", "")).casefold() == "running")
    parts = [f"{len(vms)} VM" + ("" if len(vms) == 1 else "s"), f"{running} running"]
    if host.get("profile"):
        parts.append(f"{host['profile']} profile")
    if host.get("hypervisor_present") is False:
        parts.append("Hyper-V is off")
    return " · ".join(parts)


def read_status(service: Any | None) -> tuple[str, bool]:
    """The tooltip line, and whether the host answered.

    A tray that cannot reach the service is showing the one state this icon
    exists for, so the failure is the answer rather than an exception.
    """
    if service is None:
        return "Not answering: the service is not running.", False
    try:
        return status_text(service.host_status(), service.list_vms()), True
    except HyperVError as exc:
        return f"Not answering: {exc}", False


def window_command() -> list[str]:
    """The command that opens the window, as a separate process.

    The same entry point a person types and the shortcut runs, so there is
    still one door; ``--ui`` says which half of it is wanted.
    """
    if frozen.is_frozen():
        return [str(frozen.sibling(frozen.LAUNCHER)), "--ui"]
    return [sys.executable, "-m", "hyperv_console_mcp.cli", "--ui"]


def open_window() -> None:
    """Start the window unless the one this tray started is still open.

    A window opened from somewhere else is none of the tray's business, so it
    is not counted here — this only stops one click too many from leaving two
    windows behind.
    """
    global _window
    if _window is not None and _window.poll() is None:
        return
    # Detached: the window outlives the tray, which is the point of a separate
    # process. It has no console, and it needs none — it is a window.
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    _window = subprocess.Popen(window_command(), creationflags=flags, close_fds=True)


def icon_image() -> Any:
    """A small console glyph, drawn rather than shipped as a file."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 8, 60, 56), radius=9, fill=(15, 23, 42, 255))
    draw.rectangle((11, 18, 53, 25), fill=(148, 163, 184, 255))
    draw.rectangle((11, 31, 36, 37), fill=(56, 189, 248, 255))
    draw.rectangle((11, 41, 47, 47), fill=(148, 163, 184, 255))
    return image


def write_icon(path: Path) -> Path:
    """The same glyph as an ``.ico``, for the frozen build and the installer.

    PyInstaller wants an icon file and Inno Setup wants one; both get the tray's
    own drawing instead of a second copy of it that could drift.
    """
    image = icon_image()
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    return path


def tray_service() -> tuple[Any, Any]:
    """The shared service, opened the way every other client opens it."""
    from .config import Settings
    from .service.host import open_service

    return open_service(Settings.from_env(), actor="tray")


def _set_title(icon: Any, line: str) -> None:
    """Put the line on the tooltip, where a platform that refuses it loses nothing."""
    try:
        icon.title = f"{TITLE} — {line}"
    except Exception:  # noqa: BLE001 — a tooltip is not worth ending the tray over
        pass


def _refresh(icon: Any, service: Any | None, stop: threading.Event) -> None:
    while not stop.wait(REFRESH_SECONDS):
        _set_title(icon, read_status(service)[0])
        try:
            icon.update_menu()
        except Exception:  # noqa: BLE001 — a stopped icon is the normal way out
            return


def main() -> int:
    try:
        import pystray
    except ImportError:
        print(
            'hyperv-tray needs pystray: pip install "hyperv-console-mcp[tray]"',
            file=sys.stderr,
        )
        return 2

    first_failure = "Not answering: the service is not running."
    private = None
    try:
        service, private = tray_service()
    except HyperVError as exc:
        # The tray is how an operator notices something is wrong, so a host that
        # will not answer still gets an icon that says so instead of no icon.
        service = None
        first_failure = f"Not answering: {exc}"
        print(f"{TITLE}: {exc}", file=sys.stderr)

    def describe() -> str:
        return first_failure if service is None else read_status(service)[0]

    def on_open(icon: Any, item: Any) -> None:
        open_window()

    def on_status(icon: Any, item: Any) -> None:
        line = describe()
        _set_title(icon, line)
        try:
            icon.notify(line, TITLE)
        except Exception:  # noqa: BLE001 — the tooltip already says it
            pass

    def on_quit(icon: Any, item: Any) -> None:
        icon.stop()

    icon = pystray.Icon(
        "hyperv-console",
        icon_image(),
        f"{TITLE} — {describe()}",
        menu=pystray.Menu(
            pystray.MenuItem(OPEN, on_open, default=True),
            pystray.MenuItem(STATUS, on_status),
            pystray.MenuItem(QUIT, on_quit),
        ),
    )
    stop = threading.Event()
    threading.Thread(target=_refresh, args=(icon, service, stop), daemon=True).start()
    try:
        icon.run()
    finally:
        stop.set()
        if private is not None:
            private.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
