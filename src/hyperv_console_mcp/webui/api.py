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

A third thing is also the window's: opening Hyper-V's own viewer. The service is
a background process and a window it started would belong to that process's
desktop, not to the operator's; the window is the only client that is certainly
looking at the operator's screen. What it opens is decided here and not in the
page — the VM is checked against the service's list first, so the allowlist and
the host's inventory still stand between a name and a process. The viewer takes
no credential and changes nothing, which is why it needs no approval card.

The first-run wizard is neither a new door nor a rule of its own: it reads the
VM list through the service and the credential sources through the vault, and
writes only the level (the access file above) and the password (the service's
``guest_credential_set``). What it asks, and whether it should open at all, is
decided in :mod:`hyperv_console_mcp.webui.wizard`, in Python, where it is tested.

Nothing in this module knows about ``webview``: the bridge is testable without a
window, and the window module imports it, not the other way round.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from .. import credentials
from ..access import CAPABILITIES, LEVEL_CAPABILITIES, LEVELS, AccessStore, access_file
from ..elevation import is_elevated
from ..errors import HyperVError, ValidationError
from ..service.remote import dispatch
from . import wizard as wizard_rules

# How long a tool waits for the operator before the request is denied. The
# dashboard uses the same window; a card that outlives it is dropped.
APPROVAL_TIMEOUT_SECONDS = 180.0

def _wizard_failure(step: str, message: str, code: str) -> dict[str, Any]:
    """A refusal in the envelope the page already knows, plus the step it belongs to."""
    return {"error": {"code": code, "message": message, "step": step}}


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
            # The matrix: what each VM may actually do, which is its level or the
            # subset the operator kept.
            "capabilities_by_vm": {
                name: sorted(self.settings.capabilities_for(name))
                for name in self.settings.levels
            },
            # What each level offers, so the matrix can grey out what a level
            # cannot grant instead of offering a checkbox that would be refused.
            "capabilities_by_level": {
                level: sorted(names) for level, names in LEVEL_CAPABILITIES.items()
            },
            "profile": self.settings.profile,
            "elevated": is_elevated(),
            "shared_service": self.settings.shared_service,
            "capabilities": {
                name: self.settings.allows(self.vm_name, name) if self.vm_name else False
                for name in CAPABILITIES
            },
            # Whether this VM has a credential, and which account it uses. The
            # page needs the fact to know whether to ask; it never gets the
            # secret, and nothing here reads it out of the vault.
            "credential": self.credential(),
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

    def logs(
        self,
        after: int = 0,
        vm: str = "",
        source: str = "",
        level: str = "",
        timeout: float = 10,
    ) -> dict[str, Any]:
        """The unified log stream, held open, for the window's dock.

        Same shape as ``events``: the page asks from the last sequence it drew
        and the service answers when there is a line or the wait runs out. The
        filters are the service's, so a dock that asks for one VM does not make
        the host send the others for the page to throw away.
        """
        return self.call(
            "logs_subscribe",
            {
                "after": after,
                "vm": vm or None,
                "source": source or None,
                "level": level or None,
                "timeout_seconds": timeout,
            },
        )

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

    def set_level(
        self, level: str, capabilities: list[str] | None = None
    ) -> dict[str, Any]:
        """Write one level for this VM, and optionally narrow what it allows.

        This is the same file ``hyperv-access`` writes. It is not a service
        method and no MCP tool reaches it: a level is the operator's decision
        about what the AI may do, so only the operator's own window writes it.
        ``capabilities`` is the matrix's answer — the ones the operator kept —
        and it can only take capabilities away from the level.
        """
        if not self.vm_name:
            raise ValidationError("Choose a VM before setting a level.")
        store = self._store()
        store.set(self.vm_name, level, capabilities)
        # The window tells the truth about the file it just wrote even when the
        # service cannot be told: a panel that disagrees with the policy file is
        # worse than a loud error about the service.
        self._reread()
        policy = self.service.reload_policy()
        return {
            "level": store.levels().get(self.vm_name.casefold()),
            "capabilities": sorted(self.settings.capabilities_for(self.vm_name)),
            "policy": policy,
        }

    def remove_level(self) -> dict[str, Any]:
        """Take this VM out of the file, which makes it unreachable again."""
        if not self.vm_name:
            raise ValidationError("Choose a VM before removing a level.")
        store = self._store()
        store.remove(self.vm_name)
        self._reread()
        policy = self.service.reload_policy()
        return {"level": None, "policy": policy}

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
        self._reread()
        policy = self.service.reload_policy()
        return {
            "baseline": store.baselines().get(self.vm_name.casefold()),
            "policy": policy,
        }

    def credential(self) -> dict[str, Any]:
        """Where this VM's guest credential comes from, and for which account.

        The answer is a word — ``credential_manager``, ``environment`` or
        ``missing`` — plus the username. The page draws a login screen when it
        is ``missing`` and nothing else otherwise, so reopening the window does
        not ask for a password that is already in the vault.
        """
        if not self.vm_name:
            return {"source": "missing", "username": None}
        entry = None
        try:
            entry = credentials.CredentialStore().read(self.vm_name)
        except HyperVError:
            # A vault that cannot be read is the same problem as an empty one:
            # the page must offer the login rather than fail to draw.
            entry = None
        return {
            "source": credentials.source(self.settings, self.vm_name),
            "username": (entry[0] if entry else None) or self.settings.guest_username or None,
        }

    def set_credential(self, username: str, password: str) -> dict[str, Any]:
        """Put this VM's guest credential in the Windows vault, once.

        The password is typed here and goes straight to the service, which
        writes it to the Credential Manager. It is not kept in the page's state,
        not echoed back in the answer, and not written to any log.
        """
        if not self.vm_name:
            raise ValidationError("Choose a VM before storing its credential.")
        return self.call(
            "guest_credential_set",
            {"vm_name": self.vm_name, "username": username, "password": password},
        )

    def check_credential(self) -> dict[str, Any]:
        """Ask the service whether the stored credential actually works.

        This is the slow one — it opens a session — so the page runs it on a
        button, not on every draw.
        """
        if not self.vm_name:
            raise ValidationError("Choose a VM before testing its credential.")
        return self.call("guest_credential_status", {"vm_name": self.vm_name})

    def _store(self) -> AccessStore:
        return AccessStore(self.settings.access_path or access_file(self.environ))

    def _reread(self) -> None:
        """Take the operator's file as it is now, so this window is not stale.

        The service replaces its own ``Settings`` when it reloads, but the bridge
        holds the instance it was built with: a level written here would keep
        reading as "no level" — in this window, and in the first-run wizard that
        asks the same question — until it was reopened. Only the two things the
        file holds are replaced; the environment the window started with stays.
        """
        store = self._store()
        entries = store.entries()
        self.settings = replace(
            self.settings,
            levels=store.levels(),
            baselines=store.baselines(),
            capabilities={
                key: tuple(record["capabilities"] or ())
                for key, record in entries.items()
                if record["capabilities"] is not None
            },
        )

    # -- the first run -----------------------------------------------------

    def wizard(self) -> dict[str, Any]:
        """The first-run questions, answered from what the host already knows.

        The VM list is the service's; the levels are the operator's file; the
        credential sources are the vault's. The page decides nothing: it draws
        what comes back, including whether it should open at all.
        """
        response = self.call("list_vms")
        if "error" in response:
            return response
        vms = (response.get("ok") or {}).get("vms") or []
        sources: dict[str, str] = {}
        for vm in vms:
            name = str(vm.get("name") or "").strip()
            if name:
                sources[name.casefold()] = credentials.source(self.settings, name)
        return {"ok": wizard_rules.status(
            vms=vms,
            levels=self.settings.levels,
            credentials=sources,
            username=self.settings.guest_username or "",
        )}

    def wizard_finish(
        self, vm_name: str = "", level: str = "", username: str = "", password: str = ""
    ) -> dict[str, Any]:
        """Write the three answers in the order that leaves a usable window.

        The level goes into the operator's own file first: a VM with no level is
        unreachable, so a stored password would be a secret with nothing to open.
        The password is then handed to the service, which puts it in the
        Credential Manager — it is never returned, echoed or kept here.

        A failure names the step that failed, so the page can point at the field
        instead of showing a traceback to somebody on their first run.
        """
        name = str(vm_name or "").strip()
        if not name:
            return _wizard_failure("vm", "Choose a virtual machine first.", "invalid_argument")
        previous = self.vm_name
        self.vm_name = name
        try:
            written = self.set_level(level)
        except HyperVError as error:
            self.vm_name = previous
            return _wizard_failure("level", str(error), error.code)
        stored = ""
        if password:
            account = str(username or "").strip() or (self.settings.guest_username or "")
            response = self.call(
                "guest_credential_set",
                {"vm_name": name, "username": account, "password": password},
            )
            if "error" in response:
                return {"error": {**response["error"], "step": "credential"}}
            stored = str((response.get("ok") or {}).get("username") or account)
        return {"ok": {
            "vm": name,
            "level": written.get("level"),
            "username": stored or None,
            "credential": "stored" if stored else "skipped",
        }}

    # -- Hyper-V's own windows ---------------------------------------------

    def open_vmconnect(self, vm_name: str = "") -> dict[str, Any]:
        """Open VMConnect on the operator's desktop, for full fidelity.

        The console page is a picture of the display, so the real viewer is one
        click away instead of a sentence telling the operator to type a command.
        The name is checked against ``list_vms`` first, which is both the host's
        inventory and the allowlist: a VM this window was not given is refused
        here rather than opened.
        """
        name = str(vm_name or self.vm_name or "").strip()
        if not name:
            return {"error": {
                "code": "invalid_argument",
                "message": "Choose a VM before opening VMConnect.",
            }}
        listing = self.call("list_vms")
        if "error" in listing:
            return listing
        known = {
            str(vm.get("name") or "").casefold()
            for vm in (listing.get("ok") or {}).get("vms") or []
        }
        if name.casefold() not in known:
            return {"error": {
                "code": "not_authorized",
                "message": (
                    f"{name} is not a virtual machine this host reports to this window, "
                    "so VMConnect has nothing to open."
                ),
            }}
        return self._launch("vmconnect.exe", "localhost", name)

    def open_hyperv_manager(self) -> dict[str, Any]:
        """Open Hyper-V Manager, which is where a VM is created or its hardware edited.

        This window is a specialist in the driver-test loop and does not rebuild
        the manager; it points at it, the same way the empty fleet tells the
        operator where to make the first VM.
        """
        console = self._system32("virtmgmt.msc")
        if not console.exists():
            return {"error": {
                "code": "unavailable",
                "message": (
                    f"{console} is not installed on this host, so Hyper-V Manager "
                    "cannot be opened. The Hyper-V management tools provide it."
                ),
            }}
        return self._launch("mmc.exe", str(console))

    def _system32(self, name: str) -> Path:
        root = (self.environ or os.environ).get("SystemRoot", r"C:\Windows")
        return Path(root) / "System32" / name

    def _launch(self, program: str, *arguments: str) -> dict[str, Any]:
        """Start one of Windows' own consoles, detached, and say what was started.

        Nothing but the program's name and the arguments this module built
        reaches the command line: the VM name is one argument, never spliced
        into a shell string, so a name with a space or a quote is still a name.
        """
        executable = self._system32(program)
        if not executable.exists():
            return {"error": {
                "code": "unavailable",
                "message": (
                    f"{executable} is not installed on this host, so it cannot be opened. "
                    "The Hyper-V management tools provide it."
                ),
            }}
        try:
            process = subprocess.Popen([str(executable), *arguments], close_fds=True)
        except OSError as error:
            return {"error": {
                "code": "unavailable",
                "message": f"{program} could not be started: {error}",
            }}
        return {"ok": {"program": program, "pid": process.pid, "arguments": list(arguments)}}

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
