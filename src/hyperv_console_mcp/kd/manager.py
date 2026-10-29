"""Live kd sessions, at most one per VM, plus a clean shutdown at exit."""

from __future__ import annotations

import atexit
import threading
from typing import Any, Callable

from ..errors import DebuggerError
from .session import KdSession


class KdManager:
    def __init__(self) -> None:
        self._sessions: dict[str, KdSession] = {}
        self._lock = threading.Lock()
        atexit.register(self.close)

    @staticmethod
    def _key(vm_name: str) -> str:
        return vm_name.casefold()

    def get(self, vm_name: str) -> KdSession | None:
        with self._lock:
            return self._sessions.get(self._key(vm_name))

    def require(self, vm_name: str) -> KdSession:
        session = self.get(vm_name)
        if session is None:
            raise DebuggerError(
                f"No debugger session is attached to {vm_name}. Call kd_attach first.",
                code="kd_not_connected",
            )
        return session

    def start(
        self,
        vm_name: str,
        create: Callable[[], KdSession],
        *,
        startup_timeout: float | None = None,
    ) -> KdSession:
        """Create and start a session; refuses to shadow a live one."""
        key = self._key(vm_name)
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and existing.alive:
                raise DebuggerError(
                    "A debugger session is already attached to this VM. Detach it first.",
                    code="kd_already_connected",
                )
            if existing is not None:
                existing.close()
            session = create()
            self._sessions[key] = session
        try:
            if startup_timeout is None:
                session.start()
            else:
                session.start(startup_timeout=startup_timeout)
        except Exception:
            self.stop(vm_name)
            raise
        return session

    def stop(self, vm_name: str, *, detach: bool = True) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.pop(self._key(vm_name), None)
        if session is None:
            return {"attached": False, "completed": True}
        if detach:
            status = session.detach()
        else:
            session.close()
            status = session.status()
        return {"attached": True, "completed": True, **status}

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass


__all__ = ["KdManager"]
