"""What the WebView2 page may ask the host, and nothing more.

The page is a client of the same service the MCP server uses, so the policy, the
allowlist, the audit trail and the sessions are the ones that already exist.
Everything the page calls goes through :func:`~hyperv_console_mcp.service.remote.dispatch`,
which is the envelope the pipe uses: only public methods cross, arguments are
bound to the signature first, and a failure comes back as ``{"error": {...}}``
rather than a traceback.

Two things are the window's own and are not service methods: reading and writing
the operator's access file (the same file ``hyperv-access`` writes, which no MCP
tool can reach), and answering approval requests, which arrive from the broker
on its own thread and are answered by a click here.

Nothing in this module knows about ``webview``: the bridge is testable without a
window, and the window module imports it, not the other way round.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any, Mapping

from ..access import CAPABILITIES, LEVELS, AccessStore, access_file
from ..elevation import is_elevated
from ..errors import ValidationError
from ..service.remote import dispatch

# How long a tool waits for the operator before the request is denied. The
# dashboard uses the same window; a card that outlives it is dropped.
APPROVAL_TIMEOUT_SECONDS = 180.0


class Api:
    """The object pywebview exposes to the page as ``window.pywebview.api``.

    Every method returns JSON-able data. A method may fail loudly for the
    developer, but not for the page: the service's refusals are returned as
    envelopes so the window can show the message and the call that would fix it.
    """

    actor = "ui"

    def __init__(
        self,
        service: Any,
        settings: Any,
        vm_name: str = "",
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.service = service
        self.settings = settings
        self.vm_name = vm_name
        self.environ = environ
        self.requests: dict[str, dict[str, Any]] = {}
        self.waiting: dict[str, queue.Queue[bool]] = {}
        self.lock = threading.Lock()
        # Set by the window when the approval pipe is owned by another process.
        self.approval_error: str | None = None
        self.broker: Any = None

    # -- the window's own state ------------------------------------------

    def hello(self) -> dict[str, Any]:
        """Everything the page needs before it draws anything."""
        level = self.settings.level_for(self.vm_name) if self.vm_name else None
        return {
            "vm": self.vm_name,
            "level": level,
            "levels": list(LEVELS),
            # The checkpoint the operator protects, if any: the page marks it and
            # offers no delete for it, because the service refuses that anyway.
            "baseline": self.settings.baseline_for(self.vm_name) if self.vm_name else None,
            # The fleet badges every VM with its own level, and a level is a
            # property of the VM, not of the window's current choice.
            "levels_by_vm": dict(self.settings.levels),
            "profile": self.settings.profile,
            "elevated": is_elevated(),
            "shared_service": self.settings.shared_service,
            "capabilities": {
                name: self.settings.allows(self.vm_name, name) if self.vm_name else False
                for name in CAPABILITIES
            },
            "allowlist": sorted(self.settings.allowed_vms),
            "approvals": self.approval_error or "listening",
        }

    def vms(self) -> dict[str, Any]:
        """The VMs Hyper-V reports, so the header can offer a choice."""
        return self.call("list_vms")

    def events(self, after: int = 0, timeout: float = 10) -> dict[str, Any]:
        """Wait for the next events, so the feed is live instead of polled.

        The page calls this in a loop with the sequence it has already shown;
        the service holds the call until something new arrives or the timeout
        expires. Asking again after a timeout is not an error — an empty answer
        means "nothing happened", which is the truth.
        """
        return self.call("events_wait", {"after": after, "timeout_seconds": timeout})

    def choose(self, name: str) -> dict[str, Any]:
        """Point the window at another VM, without reopening it.

        An empty name means "no VM is open", which is what closing the last
        workspace leaves behind: the page then shows the fleet and the per-VM
        pages are hidden, rather than keeping a VM the operator just closed.
        """
        self.vm_name = str(name or "").strip()
        return self.hello()

    # -- the service ------------------------------------------------------

    def call(self, method: str, kwargs: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """One service call in the same envelope the pipe carries."""
        return dispatch(
            self.service,
            {"method": method, "actor": self.actor, "kwargs": dict(kwargs or {})},
        )

    # -- the operator's access file ---------------------------------------

    def set_level(self, level: str) -> dict[str, Any]:
        """Write one level for this VM, then have the service read the file again.

        This is the same file ``hyperv-access`` writes. It is not a service
        method and no MCP tool reaches it: a level is the operator's decision
        about what the AI may do, so only the operator's own window writes it.
        """
        if not self.vm_name:
            raise ValidationError("Choose a VM before setting a level.")
        store = self._store()
        store.set(self.vm_name, level)
        return {"level": store.levels().get(self.vm_name.casefold()), "policy": self.service.reload_policy()}

    def remove_level(self) -> dict[str, Any]:
        """Take this VM out of the file, which makes it unreachable again."""
        if not self.vm_name:
            raise ValidationError("Choose a VM before removing a level.")
        store = self._store()
        store.remove(self.vm_name)
        return {"level": None, "policy": self.service.reload_policy()}

    def set_baseline(self, checkpoint: str) -> dict[str, Any]:
        """Name the checkpoint this VM must not lose, in the operator's file.

        Same rule as the level: only the operator's own window writes it. The
        service re-reads the file, so the protection is live for the AI too —
        `delete_checkpoint` refuses the baseline whoever asks.
        """
        if not self.vm_name:
            raise ValidationError("Choose a VM before naming a baseline checkpoint.")
        store = self._store()
        store.set_baseline(self.vm_name, checkpoint)
        return {
            "baseline": store.baselines().get(self.vm_name.casefold()),
            "policy": self.service.reload_policy(),
        }

    def _store(self) -> AccessStore:
        return AccessStore(self.settings.access_path or access_file(self.environ))

    # -- approvals ---------------------------------------------------------

    def request_approval(self, request: dict[str, Any]) -> bool:
        """Called by the broker's thread; returns the operator's answer.

        The request is held until the page answers it or the timeout expires,
        which is what makes the card disappear when nobody is looking.
        """
        request_id = str(request.get("request_id") or "")
        if not request_id:
            return False
        response: queue.Queue[bool] = queue.Queue(maxsize=1)
        with self.lock:
            self.requests[request_id] = dict(request)
            self.waiting[request_id] = response
        try:
            return response.get(timeout=APPROVAL_TIMEOUT_SECONDS)
        except queue.Empty:
            return False
        finally:
            with self.lock:
                self.waiting.pop(request_id, None)
                self.requests.pop(request_id, None)

    def approvals(self) -> list[dict[str, Any]]:
        """The requests still waiting, as the cards the page draws."""
        with self.lock:
            return [dict(request) for request in self.requests.values()]

    def decide(self, request_id: str, approved: bool) -> dict[str, Any]:
        """Answer one request; a second click on the same card does nothing."""
        with self.lock:
            response = self.waiting.get(request_id)
        if response is None:
            return {"ok": False, "reason": "that request is no longer waiting"}
        response.put(bool(approved))
        return {"ok": True}

    # -- convenience for the page ------------------------------------------

    def screenshot(self, options: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """A screenshot as a data URL, which is what an ``<img>`` can show.

        pywebview passes the page's arguments positionally, so the options may
        arrive as one object or as keywords; both mean the same thing.
        """
        merged = {**(dict(options) if options else {}), **kwargs}
        envelope = self.call("capture_screenshot", {"vm_name": self.vm_name, **merged})
        if "error" in envelope:
            return envelope
        shot = envelope.get("ok") or {}
        if not isinstance(shot, dict) or shot.get("__type__") != "screenshot":
            return {"error": {"code": "internal_error", "message": "the host returned no image"}}
        return {
            "ok": {
                "src": "data:image/png;base64," + str(shot.get("png") or ""),
                "metadata": shot.get("metadata") or {},
            }
        }

    def assets(self) -> Path:
        """Where the page's own files live, for a caller that wants the path."""
        return Path(__file__).with_name("assets")


__all__ = ["APPROVAL_TIMEOUT_SECONDS", "Api"]
