"""Host operations that no longer need a PowerShell worker.

The bridge still owns the guest side and every host operation not yet moved;
this package takes them one at a time, each with its mapping in Python and its
own test, while ``FallbackBackend`` keeps the worker answering the rest.
"""

from __future__ import annotations

from .backend import WmiBackend, create_backend

__all__ = ["WmiBackend", "create_backend"]
