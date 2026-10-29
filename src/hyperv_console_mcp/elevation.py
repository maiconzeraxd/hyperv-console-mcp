"""Whether this process can administer the host, asked once in one place.

Both windows show the same warning, and neither should own the check: the
question is about the process, not about tkinter or WebView2, and a second copy
would be a second answer.
"""

from __future__ import annotations

import ctypes
import os


def is_elevated() -> bool:
    """True when the process runs with administrator rights.

    Hyper-V reads fail with ``access_denied`` in an unelevated shell, which is
    worth saying in the window before the first page looks broken. On a
    non-Windows host the question has no meaning, so it answers ``False``.
    """
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


__all__ = ["is_elevated"]
