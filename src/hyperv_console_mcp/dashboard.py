"""Native Windows control center for the same policy-gated Hyper-V service used by MCP."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from .backend import PowerShellBackend
from .config import Settings
from .errors import HyperVError
from .events import EVENTS, EventSink
from .ipc import ApprovalBroker
from .lab.cli import diagnostics, initialize
from .lab.transfer import DriverLabTransfer, build_manifest
from .service import HyperVService

COLORS = {
    "background": "#0b1120", "panel": "#111b2e", "surface": "#17243a",
    "border": "#273851", "text": "#e5edf8", "muted": "#8fa5bf",
    "accent": "#6ee7b7", "blue": "#60a5fa", "warning": "#fbbf24", "error": "#fb7185",
}


def _is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _safe_json(value: Any) -> str:
    def sanitize(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: sanitize(val) for key, val in item.items() if key not in {"password", "content_base64"}}
        if isinstance(item, list):
            return [sanitize(val) for val in item]
        if isinstance(item, str) and len(item) > 16_384:
            return item[:16_384] + "… [truncated]"
        return item
    return json.dumps(sanitize(value), ensure_ascii=False, indent=2)


class Dashboard(tk.Tk):
    def __init__(self, vm_name: str) -> None:
        super().__init__()
        self.vm_name = vm_name
        self.settings = Settings.from_env()
        self.backend = EventSink(PowerShellBackend(), EVENTS)
        self.service = HyperVService(self.backend, self.settings)
        self.queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.pending: dict[str, queue.Queue[bool]] = {}
        self.pending_lock = threading.Lock()
        self.seen_events: set[str] = set()
        self.events: deque[dict[str, Any]] = deque(maxlen=1000)
        self.running = True
        self.busy = 0
        self.last_vm: dict[str, Any] = {}
        self.last_checkpoints: dict[str, Any] = {}
        self.last_media: dict[str, Any] = {}
        self.last_package: dict[str, Any] = {}
        self.current_page = "Overview"
        self.title("Hyper-V Control Center")
        self.geometry("1420x900")
        self.minsize(1080, 720)
        self.configure(bg=COLORS["background"])
        self._theme()
        self._layout()
        self.show(self.current_page)
        self.broker = ApprovalBroker(self._approval_requested)
        self.broker.start()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self._process_queue)
        self.after(500, self.refresh)
        self.after(2000, self._refresh_events)

    def _theme(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=COLORS["background"], foreground=COLORS["text"], font=("Segoe UI", 10))
        style.configure("TFrame", background=COLORS["background"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure("TLabel", background=COLORS["background"], foreground=COLORS["text"])
        style.configure("Panel.TLabel", background=COLORS["panel"], foreground=COLORS["text"])
        style.configure("Muted.TLabel", foreground=COLORS["muted"], background=COLORS["panel"])
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 24), background=COLORS["panel"])
        style.configure("Subtitle.TLabel", foreground=COLORS["muted"], background=COLORS["panel"])
        style.configure("TButton", background=COLORS["surface"], foreground=COLORS["text"], padding=(12, 8), borderwidth=0)
        style.map("TButton", background=[("active", "#243955"), ("disabled", "#152033")])
        style.configure("Accent.TButton", background="#175c4b", foreground="#d9fff1", font=("Segoe UI Semibold", 10))
        style.map("Accent.TButton", background=[("active", "#207862")])
        style.configure("Nav.TButton", anchor="w", padding=(18, 13), background=COLORS["background"], foreground=COLORS["muted"])
        style.map("Nav.TButton", background=[("active", COLORS["surface"])])
        style.configure("Treeview", background=COLORS["panel"], fieldbackground=COLORS["panel"], foreground=COLORS["text"], rowheight=30, borderwidth=0)
        style.configure("Treeview.Heading", background=COLORS["surface"], foreground=COLORS["muted"], padding=8, borderwidth=0)
        style.map("Treeview", background=[("selected", "#254769")])
        style.configure("TEntry", fieldbackground=COLORS["surface"], foreground=COLORS["text"], insertcolor=COLORS["text"], borderwidth=1)
        style.configure("TCombobox", fieldbackground=COLORS["surface"], foreground=COLORS["text"], background=COLORS["surface"], arrowcolor=COLORS["text"])
        style.configure("TLabelframe", background=COLORS["panel"], bordercolor=COLORS["border"], padding=14)
        style.configure("TLabelframe.Label", background=COLORS["panel"], foreground=COLORS["text"], font=("Segoe UI Semibold", 11))
        style.configure("Horizontal.TProgressbar", background=COLORS["accent"], troughcolor=COLORS["surface"], borderwidth=0)
        style.configure("TCheckbutton", background=COLORS["panel"], foreground=COLORS["text"])
        style.map("TCheckbutton", background=[("active", COLORS["panel"])])

    def _layout(self) -> None:
        nav = ttk.Frame(self, width=230, padding=(16, 24))
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        ttk.Label(nav, text="HYPER-V", font=("Segoe UI Semibold", 22), foreground=COLORS["accent"]).pack(anchor="w")
        ttk.Label(nav, text="Control Center", foreground=COLORS["muted"]).pack(anchor="w", pady=(0, 8))
        ttk.Label(nav, textvariable=tk.StringVar(value=self.vm_name), foreground=COLORS["blue"]).pack(anchor="w", pady=(0, 24))
        self.content = ttk.Frame(self, padding=26)
        self.content.pack(side="right", fill="both", expand=True)
        for page in ("Overview", "Activity", "Driver lab", "Console", "Recovery", "Media", "Debugger"):
            ttk.Button(nav, text=page, style="Nav.TButton", command=lambda p=page: self.show(p)).pack(fill="x", pady=3)
        ttk.Frame(nav, height=1, style="Panel.TFrame").pack(fill="x", pady=20)
        flags = []
        for label, enabled in (
            ("Power", self.settings.allow_power), ("Guest", self.settings.allow_guest),
            ("Console", self.settings.allow_console), ("Checkpoints", self.settings.allow_checkpoints),
            ("Media", self.settings.allow_media), ("Debugger", self.settings.allow_debugger),
        ):
            flags.append(("● " if enabled else "○ ") + label)
        self.capabilities = tk.StringVar(value="\n".join(flags))
        ttk.Label(nav, textvariable=self.capabilities, foreground=COLORS["muted"], justify="left").pack(anchor="w")
        self.elevation = tk.StringVar(value="Elevated host" if _is_elevated() else "Read permission may be limited")
        ttk.Label(nav, textvariable=self.elevation, foreground=COLORS["accent"] if _is_elevated() else COLORS["warning"], wraplength=180).pack(side="bottom", anchor="w", pady=20)

    def show(self, page: str) -> None:
        self.current_page = page
        for child in self.content.winfo_children():
            child.destroy()
        header = ttk.Frame(self.content, style="Panel.TFrame", padding=(22, 18))
        header.pack(fill="x", pady=(0, 16))
        descriptions = {
            "Overview": ("Laboratory at a glance", "Live host, virtual machine, security and recovery state."),
            "Activity": ("What the AI is doing", "Cross-process operation history, progress and local approval requests."),
            "Driver lab": ("Packages, runs and evidence", "Reproducible staging with integrity checks. Staging never executes a payload."),
            "Console": ("Interactive guest console", "Real synthetic input to the guest. Confirm every action."),
            "Recovery": ("Checkpoints and recovery", "Inspect checkpoints. Restore and delete require a separate local confirmation."),
            "Media": ("Installation media", "Mount or eject an ISO on an existing DVD drive while the VM is Off."),
            "Debugger": ("Kernel debugging and serial", "Separate host/guest preparation; no automatic stop, reboot, or debugger attachment."),
        }
        title, subtitle = descriptions[page]
        ttk.Label(header, text=title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text=subtitle, style="Subtitle.TLabel").pack(anchor="w", pady=(4, 0))
        getattr(self, "_page_" + page.lower().replace(" ", "_"))()

    def _text(self, parent: Any, height: int = 12) -> tk.Text:
        frame = ttk.Frame(parent, style="Panel.TFrame")
        widget = tk.Text(frame, height=height, wrap="word", bg=COLORS["panel"], fg=COLORS["text"], insertbackground=COLORS["text"], selectbackground="#254769", relief="flat", padx=14, pady=12, font=("Cascadia Mono", 9))
        scroll = ttk.Scrollbar(frame, command=widget.yview)
        widget.configure(yscrollcommand=scroll.set)
        widget.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return frame, widget

    def _write_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _page_overview(self) -> None:
        cards = ttk.Frame(self.content)
        cards.pack(fill="x")
        self.state_card = self._card(cards, "Virtual machine", "Reading…", 0)
        self.host_card = self._card(cards, "Host", "Reading…", 1)
        self.recovery_card = self._card(cards, "Recovery", "Reading…", 2)
        self.guest_card = self._card(cards, "Guest access", "Not queried", 3)
        row = ttk.Frame(self.content)
        row.pack(fill="both", expand=True, pady=(16, 0))
        details = ttk.LabelFrame(row, text="Live configuration")
        details.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.overview_frame, self.overview_text = self._text(details, 22)
        self.overview_frame.pack(fill="both", expand=True)
        actions = ttk.LabelFrame(row, text="Deliberate actions")
        actions.pack(side="left", fill="y", padx=(8, 0))
        for text, command in (
            ("Refresh all", self.refresh),
            ("Start VM", lambda: self.mutate("Start this VM?", lambda: self.service.power(self.vm_name, "start"))),
            ("Graceful shutdown", lambda: self.mutate("Request a graceful guest shutdown?", lambda: self.service.power(self.vm_name, "shutdown"))),
            ("Pause / resume", self._pause_resume),
            ("Create recovery checkpoint", self._checkpoint),
            ("Manage checkpoints", lambda: self.show("Recovery")),
            ("Manage installation media", lambda: self.show("Media")),
            ("Read guest diagnostics", self._guest_diagnostics),
        ):
            ttk.Button(actions, text=text, command=command, width=32).pack(pady=5, padx=4)
        self.status_var = tk.StringVar(value="Ready. No background operation is running.")
        ttk.Label(self.content, textvariable=self.status_var, foreground=COLORS["muted"]).pack(anchor="w", pady=14)

    def _card(self, parent: Any, title: str, value: str, column: int) -> tk.StringVar:
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=16)
        frame.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 6, 0 if column == 3 else 6))
        parent.columnconfigure(column, weight=1)
        ttk.Label(frame, text=title, style="Muted.TLabel").pack(anchor="w")
        variable = tk.StringVar(value=value)
        ttk.Label(frame, textvariable=variable, style="Panel.TLabel", font=("Segoe UI Semibold", 14), wraplength=240).pack(anchor="w", pady=(8, 0))
        return variable

    def _page_activity(self) -> None:
        controls = ttk.Frame(self.content)
        controls.pack(fill="x", pady=(0, 12))
        self.approval_mode = tk.StringVar()
        self._update_approval_label()
        ttk.Label(controls, textvariable=self.approval_mode, foreground=COLORS["warning"]).pack(side="left")
        ttk.Button(controls, text="Clear view", command=self._clear_activity).pack(side="right")
        columns = ("time", "actor", "operation", "vm", "phase", "error")
        self.activity = ttk.Treeview(self.content, columns=columns, show="headings")
        widths = (95, 90, 230, 170, 100, 160)
        for column, width in zip(columns, widths):
            self.activity.heading(column, text=column.title())
            self.activity.column(column, width=width, anchor="w")
        self.activity.tag_configure("failed", foreground=COLORS["error"])
        self.activity.tag_configure("progress", foreground=COLORS["blue"])
        self.activity.tag_configure("completed", foreground=COLORS["accent"])
        self.activity.pack(fill="both", expand=True)
        ttk.Label(self.content, text="Event records exclude commands, credentials, typed text and file contents.", foreground=COLORS["muted"]).pack(anchor="w", pady=12)
        self._render_activity()

    def _page_driver_lab(self) -> None:
        form = ttk.LabelFrame(self.content, text="Package workflow")
        form.pack(fill="x")
        self.package_path = tk.StringVar(value=str(self.settings.lab_root.expanduser()))
        self.package_id = tk.StringVar()
        self.manifest_hash = tk.StringVar()
        self.run_id = tk.StringVar(value=time.strftime("run-%Y%m%d-%H%M%S"))
        self.include_dump = tk.BooleanVar(value=False)
        for row, (label, variable) in enumerate((
            ("Host package directory", self.package_path), ("Package ID", self.package_id),
            ("Manifest SHA-256", self.manifest_hash), ("Run ID", self.run_id),
        )):
            ttk.Label(form, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="w", pady=6)
            ttk.Entry(form, textvariable=variable, width=74).grid(row=row, column=1, sticky="ew", padx=10, pady=6)
        form.columnconfigure(1, weight=1)
        ttk.Button(form, text="Choose directory", command=self._choose_package).grid(row=0, column=2)
        buttons = ttk.Frame(form, style="Panel.TFrame")
        buttons.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(12, 2))
        for text, command in (
            ("1. Initialize guest", self._init_lab), ("2. Stage package", self._stage),
            ("3. Verify package", self._verify), ("Checkpoint", self._checkpoint),
            ("Collect evidence", self._collect), ("Manual run instructions", self._manual_run),
        ):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 6), pady=4)
        ttk.Checkbutton(form, text="Include full kernel dump in evidence (potentially very large)", variable=self.include_dump).grid(row=5, column=0, columnspan=3, sticky="w", pady=8)
        self.lab_progress = ttk.Progressbar(self.content, maximum=100)
        self.lab_progress.pack(fill="x", pady=(14, 0))
        frame, self.lab_output = self._text(self.content, 18)
        frame.pack(fill="both", expand=True, pady=14)
        self._write_text(self.lab_output, "No payload is executed by initialization, staging, verification or collection.\nThe manual runner requires an elevated guest operator and an exact approval phrase.")

    def _page_console(self) -> None:
        row = ttk.Frame(self.content)
        row.pack(fill="x")
        self.console_keys = tk.StringVar(value="enter")
        self.console_x = tk.StringVar(value="512")
        self.console_y = tk.StringVar(value="384")
        for label, variable in (("Key chord (comma-separated)", self.console_keys), ("Guest X", self.console_x), ("Guest Y", self.console_y)):
            ttk.Label(row, text=label).pack(side="left", padx=(0, 6))
            ttk.Entry(row, textvariable=variable, width=22).pack(side="left", padx=(0, 14))
        actions = ttk.Frame(self.content)
        actions.pack(fill="x", pady=14)
        for text, command in (
            ("Send key chord", self._send_keys), ("Click guest coordinates", self._click),
            ("Ctrl+Alt+Delete", lambda: self.mutate("Send Ctrl+Alt+Delete to the guest?", lambda: self.service.console_ctrl_alt_delete(self.vm_name))),
            ("Read serial", lambda: self.submit("serial.read", lambda: self.service.serial_read(self.vm_name))),
            ("Disconnect serial", lambda: self.submit("serial.disconnect", lambda: self.service.serial_disconnect(self.vm_name))),
        ):
            ttk.Button(actions, text=text, command=command).pack(side="left", padx=(0, 6))
        ttk.Label(self.content, text="Text is sent through Hyper-V synthetic input, not clipboard or networking.").pack(anchor="w")
        self.console_text = tk.Text(self.content, height=14, bg=COLORS["panel"], fg=COLORS["text"], insertbackground=COLORS["text"], relief="flat", padx=14, pady=14, font=("Cascadia Mono", 10))
        self.console_text.pack(fill="both", expand=True, pady=12)
        ttk.Button(self.content, text="Type text into guest", style="Accent.TButton", command=self._type_text).pack(anchor="e")
        ttk.Label(self.content, text="Direct VMConnect screenshot remains experimental and is not used as an implicit desktop capture.", foreground=COLORS["muted"], wraplength=1000).pack(anchor="w", pady=16)

    def _page_recovery(self) -> None:
        controls = ttk.Frame(self.content)
        controls.pack(fill="x", pady=(0, 12))
        for text, command in (
            ("Refresh", self._refresh_checkpoints), ("Create checkpoint", self._checkpoint),
            ("Restore selected", self._restore_checkpoint),
            ("Delete selected", self._delete_checkpoint),
        ):
            ttk.Button(controls, text=text, command=command).pack(side="left", padx=(0, 6))
        columns = ("name", "id", "created", "parent")
        self.checkpoint_tree = ttk.Treeview(self.content, columns=columns, show="headings", selectmode="browse")
        for column, width in zip(columns, (250, 290, 220, 290)):
            self.checkpoint_tree.heading(column, text=column.title())
            self.checkpoint_tree.column(column, width=width, anchor="w")
        self.checkpoint_tree.pack(fill="both", expand=True)
        ttk.Label(self.content, text="Restore can discard the current VM state. Delete permanently removes the selected checkpoint.", foreground=COLORS["warning"], wraplength=1000).pack(anchor="w", pady=12)
        self._render_checkpoints()

    def _refresh_checkpoints(self) -> None:
        self.submit("checkpoint.list", lambda: self.service.list_checkpoints(self.vm_name), self._show_checkpoints)

    def _show_checkpoints(self, result: Any) -> None:
        self.last_checkpoints = result
        self._render_checkpoints()

    def _render_checkpoints(self) -> None:
        if not hasattr(self, "checkpoint_tree") or not self.checkpoint_tree.winfo_exists():
            return
        self.checkpoint_tree.delete(*self.checkpoint_tree.get_children())
        for checkpoint in self.last_checkpoints.get("checkpoints", []):
            self.checkpoint_tree.insert("", "end", iid=checkpoint["id"], values=(
                checkpoint.get("name", ""), checkpoint["id"], checkpoint.get("creation_time", ""),
                checkpoint.get("parent_snapshot_id") or "",
            ))

    def _selected_checkpoint(self) -> str | None:
        selection = self.checkpoint_tree.selection()
        if not selection:
            messagebox.showinfo("Select a checkpoint", "Select exactly one checkpoint first.", parent=self)
            return None
        return selection[0]

    def _restore_checkpoint(self) -> None:
        snapshot = self._selected_checkpoint()
        if snapshot:
            self.mutate(
                f"Restore checkpoint {snapshot}?\n\nThe current VM state may be discarded.",
                lambda: self.service.restore_checkpoint(self.vm_name, snapshot, self.vm_name, snapshot),
                lambda _: self.refresh(),
            )

    def _delete_checkpoint(self) -> None:
        snapshot = self._selected_checkpoint()
        if snapshot:
            self.mutate(
                f"Permanently delete checkpoint {snapshot}?",
                lambda: self.service.delete_checkpoint(self.vm_name, snapshot, self.vm_name, snapshot),
                lambda _: self.refresh(),
            )

    def _page_media(self) -> None:
        controls = ttk.Frame(self.content)
        controls.pack(fill="x", pady=(0, 12))
        for text, command in (
            ("Refresh", self._refresh_media), ("Choose ISO", self._choose_iso),
            ("Mount ISO", self._mount_iso), ("Eject selected", self._eject_iso),
        ):
            ttk.Button(controls, text=text, command=command).pack(side="left", padx=(0, 6))
        self.iso_path = tk.StringVar()
        self.boot_first = tk.BooleanVar(value=False)
        form = ttk.LabelFrame(self.content, text="Host ISO and existing DVD drive")
        form.pack(fill="x", pady=(0, 12))
        ttk.Label(form, text="ISO path", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.iso_path, width=90).grid(row=0, column=1, columnspan=3, sticky="ew", padx=10)
        ttk.Label(form, text="Controller", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=6)
        self.media_controller = tk.StringVar(value="0")
        self.media_location = tk.StringVar(value="0")
        ttk.Entry(form, textvariable=self.media_controller, width=8).grid(row=1, column=1, sticky="w", padx=10)
        ttk.Label(form, text="Location", style="Panel.TLabel").grid(row=1, column=2, sticky="e")
        ttk.Entry(form, textvariable=self.media_location, width=8).grid(row=1, column=3, sticky="w", padx=10)
        ttk.Checkbutton(form, text="Set this DVD as first boot device", variable=self.boot_first).grid(row=2, column=0, columnspan=4, sticky="w", pady=6)
        form.columnconfigure(1, weight=1)
        self.media_tree = ttk.Treeview(self.content, columns=("controller", "location", "mounted", "path"), show="headings", selectmode="browse", height=7)
        for column, width in zip(("controller", "location", "mounted", "path"), (100, 100, 100, 700)):
            self.media_tree.heading(column, text=column.title())
            self.media_tree.column(column, width=width, anchor="w")
        self.media_tree.pack(fill="x")
        self.media_tree.bind("<<TreeviewSelect>>", self._media_selected)
        self.media_frame, self.media_output = self._text(self.content, 10)
        self.media_frame.pack(fill="both", expand=True, pady=12)
        self._write_text(self.media_output, "The VM must already be Off. This application never stops it implicitly.")
        self._render_media()

    def _refresh_media(self) -> None:
        self.submit("media.list", lambda: self.service.media_list(self.vm_name), self._show_media)

    def _show_media(self, result: Any) -> None:
        self.last_media = result
        self._render_media()
        if hasattr(self, "media_output") and self.media_output.winfo_exists():
            self._write_text(self.media_output, _safe_json(result))

    def _render_media(self) -> None:
        if not hasattr(self, "media_tree") or not self.media_tree.winfo_exists():
            return
        self.media_tree.delete(*self.media_tree.get_children())
        for drive in self.last_media.get("dvd_drives", []):
            iid = f"{drive['controller_number']}:{drive['controller_location']}"
            self.media_tree.insert("", "end", iid=iid, values=(
                drive["controller_number"], drive["controller_location"], drive.get("mounted"), drive.get("path") or "",
            ))

    def _media_selected(self, _: Any = None) -> None:
        selection = self.media_tree.selection()
        if selection:
            controller, location = selection[0].split(":", 1)
            self.media_controller.set(controller)
            self.media_location.set(location)

    def _choose_iso(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self, initialdir=str(self.settings.iso_root), filetypes=(("ISO images", "*.iso"),)
        )
        if selected:
            self.iso_path.set(selected)

    def _media_slot(self) -> tuple[int, int] | None:
        try:
            return int(self.media_controller.get()), int(self.media_location.get())
        except ValueError:
            messagebox.showerror("Invalid DVD slot", "Controller and location must be integers.", parent=self)
            return None

    def _mount_iso(self) -> None:
        slot = self._media_slot()
        if slot and self.iso_path.get():
            controller, location = slot
            self.mutate(
                f"Mount {self.iso_path.get()} at controller {controller}, location {location}?\n\nThe VM must already be Off. Boot first: {self.boot_first.get()}",
                lambda: self.service.media_mount(self.vm_name, self.iso_path.get(), location, self.vm_name, self.boot_first.get(), controller),
                lambda _: self.refresh(),
            )
        elif slot:
            messagebox.showinfo("Choose an ISO", "Choose a host ISO image first.", parent=self)

    def _eject_iso(self) -> None:
        slot = self._media_slot()
        if slot:
            controller, location = slot
            self.mutate(
                f"Eject media at controller {controller}, location {location}?\n\nThe VM must already be Off.",
                lambda: self.service.media_eject(self.vm_name, location, self.vm_name, controller),
                lambda _: self.refresh(),
            )

    def _page_debugger(self) -> None:
        self.pipe_name = tk.StringVar(value="north-kd")
        frame = ttk.LabelFrame(self.content, text="COM1 / named pipe")
        frame.pack(fill="x")
        ttk.Label(frame, text="Pipe name", style="Panel.TLabel").pack(side="left")
        ttk.Entry(frame, textvariable=self.pipe_name, width=36).pack(side="left", padx=12)
        ttk.Button(frame, text="Read status", command=lambda: self.submit("debug.status", lambda: self.service.debug_serial_status(self.vm_name), self._show_debug)).pack(side="left", padx=6)
        ttk.Button(frame, text="Prepare host COM1 (VM Off)", command=self._debug_host).pack(side="left", padx=6)
        ttk.Button(frame, text="Prepare guest BCD (Running)", command=self._debug_guest).pack(side="left", padx=6)
        self.debug_frame, self.debug_output = self._text(self.content, 17)
        self.debug_frame.pack(fill="both", expand=True, pady=16)
        serial = ttk.LabelFrame(self.content, text="Serial console — cannot share the pipe with WinDbg")
        serial.pack(fill="x")
        ttk.Button(serial, text="Attach existing COM1 pipe", command=self._serial_connect).pack(side="left", padx=6)
        ttk.Button(serial, text="Read retained output", command=lambda: self.submit("serial.read", lambda: self.service.serial_read(self.vm_name), self._show_debug)).pack(side="left", padx=6)
        ttk.Button(serial, text="Disconnect", command=lambda: self.submit("serial.disconnect", lambda: self.service.serial_disconnect(self.vm_name), self._show_debug)).pack(side="left", padx=6)
        self.serial_log = tk.StringVar(value=str(self.settings.serial_log_root / "serial.log"))
        ttk.Label(self.content, text="Local serial log").pack(anchor="w", pady=(12, 0))
        ttk.Entry(self.content, textvariable=self.serial_log).pack(fill="x")
        self.serial_input = tk.Text(self.content, height=4, bg=COLORS["panel"], fg=COLORS["text"], insertbackground=COLORS["text"], relief="flat", padx=12, pady=10, font=("Cascadia Mono", 10))
        self.serial_input.pack(fill="x", pady=12)
        self.serial_newline = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.content, text="Append CRLF", variable=self.serial_newline).pack(side="left")
        ttk.Button(self.content, text="Send serial text", style="Accent.TButton", command=self._serial_send).pack(side="right")

    def submit(self, name: str, function: Callable[[], Any], callback: Callable[[Any], None] | None = None) -> None:
        self.busy += 1
        if hasattr(self, "status_var"):
            self.status_var.set(f"Running {name}…")
        def work() -> None:
            try:
                with EVENTS.actor("dashboard"):
                    result = function()
                self.queue.put(("result", (name, result, callback, None)))
            except Exception as exc:
                self.queue.put(("result", (name, None, callback, exc)))
        threading.Thread(target=work, daemon=True, name="hvmcp-dashboard").start()

    def mutate(self, prompt: str, function: Callable[[], Any], callback: Callable[[Any], None] | None = None) -> None:
        if not messagebox.askyesno("Confirm one operation", prompt + f"\n\nVM: {self.vm_name}\nThis approval applies only to this operation.", parent=self):
            return
        self.submit("dashboard.mutation", function, callback)

    def refresh(self) -> None:
        if self.busy:
            return
        def query() -> dict[str, Any]:
            return {"host": self.service.host_status(), "vm": self.service.get_vm(self.vm_name), "checkpoints": self.service.list_checkpoints(self.vm_name), "debug": self.service.debug_serial_status(self.vm_name), "media": self.service.media_list(self.vm_name)}
        self.submit("inventory.refresh", query, self._show_overview)

    def _show_overview(self, data: dict[str, Any]) -> None:
        self.last_vm = data["vm"]["vm"]
        self.last_checkpoints = data["checkpoints"]
        self.last_media = data.get("media", {})
        self._render_checkpoints()
        self._render_media()
        if not hasattr(self, "overview_text") or not self.overview_text.winfo_exists():
            return
        vm = self.last_vm
        self.state_card.set(f"{vm['name']} · {vm['state']}\n{vm['processor_count']} vCPU · {vm['memory_startup_bytes'] // 2**30} GiB")
        host = data["host"]
        self.host_card.set(f"Hypervisor: {host['hypervisor_present']}\n{host['logical_processor_count']} logical processors")
        self.recovery_card.set(f"{data['checkpoints']['count']} checkpoint(s)\nAutomatic: {vm['automatic_checkpoints_enabled']}")
        self.guest_card.set("PowerShell Direct\nQuery on demand")
        self._write_text(self.overview_text, _safe_json(data))

    def _guest_diagnostics(self) -> None:
        def done(result: Any) -> None:
            if hasattr(self, "overview_text") and self.overview_text.winfo_exists():
                self._write_text(self.overview_text, _safe_json(result))
                self.guest_card.set("Guest diagnostics\nRead completed")
        self.submit("guest.diagnostics", lambda: diagnostics(self.service, self.vm_name), done)

    def _pause_resume(self) -> None:
        action = "resume" if self.last_vm.get("state") == "Paused" else "pause"
        self.mutate(f"{action.title()} this VM?", lambda: self.service.power(self.vm_name, action), lambda _: self.refresh())

    def _checkpoint(self) -> None:
        name = f"lab-before-{self.run_id.get()}" if hasattr(self, "run_id") else "lab-manual-checkpoint"
        self.mutate(f"Create checkpoint {name!r}?\n\nThis writes VM state and consumes host disk space.", lambda: self.service.create_checkpoint(self.vm_name, name), lambda _: self.refresh())

    def _init_lab(self) -> None:
        self.mutate("Install the fixed lab tools and directories in C:\\DriverLab?\n\nNo staged payload will be executed.", lambda: initialize(self.service, self.vm_name), self._show_lab)

    def _choose_package(self) -> None:
        selected = filedialog.askdirectory(parent=self, initialdir=self.package_path.get())
        if selected:
            self.package_path.set(selected)

    def _stage(self) -> None:
        try:
            manifest = build_manifest(Path(self.package_path.get()), self.package_id.get() or None)
            self.package_id.set(manifest["package_id"])
            from .lab.transfer import manifest_hash
            self.manifest_hash.set(manifest_hash(manifest))
        except (HyperVError, OSError) as exc:
            messagebox.showerror("Invalid package", str(exc), parent=self)
            return
        self.mutate(f"Stage package {manifest['package_id']} in {self.vm_name}?\n\nAll files will be hash-verified. Nothing will be executed.", lambda: DriverLabTransfer(self.service).stage(self.vm_name, Path(self.package_path.get()), manifest), self._show_lab)

    def _verify(self) -> None:
        self.submit("package.verify", lambda: DriverLabTransfer(self.service).verify(self.vm_name, self.package_id.get(), self.manifest_hash.get()), self._show_lab)

    def _collect(self) -> None:
        output = self.settings.evidence_root.expanduser()
        include = self.include_dump.get()
        self.mutate(f"Collect logs, events and crash evidence for {self.run_id.get()}?\n\nFull kernel dump: {include}\nNo new test will be started.", lambda: DriverLabTransfer(self.service).collect(self.vm_name, self.package_id.get(), self.run_id.get(), output, include_kernel_dump=include), self._show_lab)

    def _manual_run(self) -> None:
        command = f"& 'C:\\DriverLab\\Tools\\Invoke-DriverLabRun.ps1' -PackageId '{self.package_id.get()}' -RunId '{self.run_id.get()}' -ExpectedManifestSha256 '{self.manifest_hash.get()}' -ApproveExecution 'EXECUTE {self.package_id.get()} {self.run_id.get()}'"
        messagebox.showinfo("Manual guest-only execution", "This application will not execute the runner or load a driver.\n\nIn an elevated guest PowerShell console, after reviewing the package and creating a checkpoint, run:\n\n" + command + "\n\nThe package must provide its own Run-Lab.ps1. Do not use a host PC for a BYOVD test.", parent=self)
        self._write_text(self.lab_output, command)

    def _show_lab(self, result: Any) -> None:
        if hasattr(self, "lab_output") and self.lab_output.winfo_exists():
            self._write_text(self.lab_output, _safe_json(result))
        if isinstance(result, dict) and "manifest_sha256" in result and hasattr(self, "manifest_hash"):
            self.manifest_hash.set(result["manifest_sha256"])

    def _send_keys(self) -> None:
        keys = [key.strip().casefold() for key in self.console_keys.get().split(",") if key.strip()]
        self.mutate(f"Send key chord {keys} to the guest?", lambda: self.service.console_press_keys(self.vm_name, keys))

    def _click(self) -> None:
        x, y = int(self.console_x.get()), int(self.console_y.get())
        self.mutate(f"Click guest display at ({x}, {y})?", lambda: self.service.console_click(self.vm_name, "left", x, y))

    def _type_text(self) -> None:
        text = self.console_text.get("1.0", "end-1c")
        self.mutate(f"Type {len(text)} characters through synthetic keyboard input?", lambda: self.service.console_type_text(self.vm_name, text))

    def _debug_host(self) -> None:
        self.mutate("Configure COM1 for kernel debugging?\n\nThe VM must already be Off. No shutdown, reboot or BCD change is automatic.", lambda: self.service.debug_serial_prepare(self.vm_name, self.pipe_name.get(), self.vm_name), self._show_debug)

    def _debug_guest(self) -> None:
        self.mutate("Enable COM1/115200 debugging in the guest BCD?\n\nA deliberate later reboot is required.", lambda: self.service.debug_guest_prepare(self.vm_name, self.vm_name), self._show_debug)

    def _serial_connect(self) -> None:
        self.mutate("Attach the serial reader to the existing COM1 pipe?\n\nDisconnect WinDbg from this pipe first.", lambda: self.service.serial_connect(self.vm_name, self.pipe_name.get(), self.serial_log.get(), self.vm_name), self._show_debug)

    def _serial_send(self) -> None:
        text = self.serial_input.get("1.0", "end-1c")
        self.mutate(
            f"Send {len(text)} characters to the guest serial console?\n\nAppend CRLF: {self.serial_newline.get()}",
            lambda: self.service.serial_send(self.vm_name, text, self.serial_newline.get()),
            self._show_debug,
        )

    def _show_debug(self, result: Any) -> None:
        if hasattr(self, "debug_output") and self.debug_output.winfo_exists():
            self._write_text(self.debug_output, _safe_json(result))

    def _approval_requested(self, request: dict[str, Any]) -> bool:
        response: queue.Queue[bool] = queue.Queue(maxsize=1)
        with self.pending_lock:
            self.pending[request["request_id"]] = response
        self.queue.put(("approval", request))
        try:
            return response.get(timeout=180)
        except queue.Empty:
            return False
        finally:
            with self.pending_lock:
                self.pending.pop(request["request_id"], None)

    def _update_approval_label(self) -> None:
        if hasattr(self, "approval_mode"):
            enabled = os.environ.get("HYPERV_MCP_REQUIRE_DASHBOARD_APPROVAL", "false").casefold() in {"1", "true", "yes", "on"}
            self.approval_mode.set("AI mutations require dashboard approval" if enabled else "AI approval gate is not required by this environment")

    def _clear_activity(self) -> None:
        self.events.clear()
        self.seen_events.clear()
        self._render_activity()

    def _refresh_events(self) -> None:
        if not self.running:
            return
        def read() -> None:
            records = []
            files = sorted(self.settings.event_directory.expanduser().glob("*.json"))
            for path in files[-1000:]:
                try:
                    records.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
            records.extend(EVENTS.snapshot()["events"])
            self.queue.put(("events", records))
        threading.Thread(target=read, daemon=True, name="hvmcp-event-reader").start()
        self.after(1500, self._refresh_events)

    def _render_activity(self) -> None:
        if not hasattr(self, "activity") or not self.activity.winfo_exists():
            return
        self.activity.delete(*self.activity.get_children())
        for event in list(self.events)[-500:]:
            self.activity.insert("", "end", iid=event["event_id"], values=(event["started_utc"][11:19], event["actor"], event["operation"], event["vm_name"] or "host", event["phase"] + (f" {event['progress']}%" if event["progress"] is not None and event["phase"] == "progress" else ""), event["error_code"] or ""), tags=(event["phase"],))
        children = self.activity.get_children()
        if children:
            self.activity.see(children[-1])

    def _process_queue(self) -> None:
        try:
            while True:
                kind, value = self.queue.get_nowait()
                if kind == "result":
                    name, result, callback, error = value
                    self.busy = max(0, self.busy - 1)
                    if hasattr(self, "status_var"):
                        self.status_var.set(f"{name}: failed — {error}" if error else f"{name}: completed")
                    if error:
                        messagebox.showerror("Operation failed", str(error), parent=self)
                    elif callback:
                        callback(result)
                elif kind == "approval":
                    request = value
                    approved = messagebox.askyesno("AI requests approval", f"Operation: {request['operation']}\nVM: {request['vm_name'] or 'host'}\n\nThe MCP server is waiting. Approve this one operation?\n\nReview the originating conversation for parameters. No payload, command or credentials are sent through this broker.", parent=self)
                    with self.pending_lock:
                        pending = self.pending.get(request["request_id"])
                    if pending:
                        pending.put(approved)
                elif kind == "events":
                    for event in value:
                        if event.get("event_id") not in self.seen_events:
                            self.seen_events.add(event["event_id"])
                            self.events.append(event)
                    if self.current_page == "Activity":
                        self._render_activity()
                    progress = [event for event in self.events if event["phase"] == "progress" and event["operation"] == "package.stage"]
                    if progress and hasattr(self, "lab_progress") and self.lab_progress.winfo_exists():
                        self.lab_progress["value"] = progress[-1]["progress"] or 0
        except queue.Empty:
            pass
        if self.running:
            self.after(100, self._process_queue)

    def close(self) -> None:
        if self.busy and not messagebox.askyesno("Operations still running", "Close the dashboard? Guest commands may still continue; closing does not stop a VM or cancel a running command.", parent=self):
            return
        self.running = False
        self.broker.close()
        self.backend.close()
        self.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", default="NORTH-Test")
    arguments = parser.parse_args()
    Dashboard(arguments.vm).mainloop()


if __name__ == "__main__":
    main()
