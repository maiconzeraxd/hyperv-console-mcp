"""The hash-verified driver lab: init, stage, run, cycle and evidence."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from ..errors import PolicyError, ValidationError
from ._base import ServiceBase, _integer, _text


class LabService(ServiceBase):
    def _tracked(
        self, operation: str, vm_name: str, action: Callable[[Any], Any]
    ) -> Any:
        """Run one long step as a single operation in the live feed.

        A run or a cycle is one blocking service call, so without this the feed
        would show a started/completed pair and nothing in between. The phases
        the step reports are the steps it actually takes, published on the same
        bus the window and the AI already watch; when another operation already
        owns the feed, the phases are dropped rather than orphaned under an
        empty correlation id.
        """
        from ..events import EVENTS, ProgressReporter

        with EVENTS.operation(operation, vm_name) as correlation:
            reporter = (
                ProgressReporter(EVENTS, operation, vm_name, correlation)
                if correlation
                else None
            )
            return action(reporter.update if reporter is not None else None)

    def lab_receipts(
        self,
        action: str = "list",
        run_ids: list[str] | None = None,
        vm_name: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """What this host kept from earlier runs: list them, open one, compare two, export all.

        A read of the evidence root, not of a VM. ``lab.report`` wrote one record
        per cycle beside its evidence; this reads those records back so a window
        can show a receipt without a second implementation of what it means.
        ``export`` copies the whole history into one timestamped file under
        ``exports/``; it copies and never moves, so no receipt stops resolving.
        """
        from ..lab import receipts

        if action not in {"list", "get", "compare", "export"}:
            raise ValidationError("action must be list, get, compare, or export.")
        if vm_name:
            self._vm(vm_name)
        root = self.settings.evidence_root.expanduser().resolve()
        if action == "list":
            return receipts.list_receipts(
                root,
                vm_name=vm_name or None,
                limit=_integer(limit, "limit", 1, receipts.MAX_RECEIPTS),
            )
        if action == "export":
            return receipts.export(
                root,
                vm_name=vm_name or None,
                limit=_integer(limit, "limit", 1, receipts.MAX_RECEIPTS),
            )
        names = list(run_ids) if isinstance(run_ids, (list, tuple)) else []
        if action == "get":
            if len(names) != 1:
                raise ValidationError("action='get' needs exactly one run id in run_ids.")
            return receipts.get_receipt(root, names[0])
        if len(names) != 2:
            raise ValidationError("action='compare' needs exactly two run ids in run_ids.")
        return receipts.compare(root, names[0], names[1])

    def lab_init(self, vm_name: str, confirm_vm: str) -> dict[str, Any]:
        from ..lab.cli import initialize
        name = self._guest_vm(vm_name)
        self._confirm(confirm_vm, name, "confirm_vm")
        with self._approve("lab_init", name, {"guest_root": r"C:\DriverLab", "installs_tools": True}):
            return initialize(self, name)

    def lab_diagnostics(self, vm_name: str) -> dict[str, Any]:
        from ..lab.cli import diagnostics
        return diagnostics(self, vm_name)

    def lab_stage(self, vm_name: str, source_directory: str, package_id: str | None = None) -> dict[str, Any]:

        from ..lab.transfer import DriverLabTransfer, build_manifest, manifest_hash
        name = self._guest_vm(vm_name)
        root = self.settings.lab_root.expanduser().resolve()
        source = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(
            _text(source_directory, "source_directory", 32_768)
        )))).resolve()
        if not source.is_relative_to(root) or source == root:
            raise PolicyError(
                f"The lab source must be a subdirectory of HYPERV_MCP_LAB_ROOT ({root})."
            )
        manifest = build_manifest(source, package_id)
        summary = {
            "package_id": manifest["package_id"],
            "source": str(source),
            "files": len(manifest["files"]),
            "bytes": sum(int(entry["size"]) for entry in manifest["files"]),
            "manifest_sha256": manifest_hash(manifest),
        }
        with self._approve("lab_stage", name, summary):
            return DriverLabTransfer(self).stage(name, source, manifest)

    def lab_verify(self, vm_name: str, package_id: str, expected_manifest_sha256: str) -> dict[str, Any]:
        from ..lab.transfer import DriverLabTransfer
        return DriverLabTransfer(self).verify(vm_name, package_id, expected_manifest_sha256)

    def lab_collect(
        self, vm_name: str, package_id: str, run_id: str,
        include_kernel_dump: bool = False,
    ) -> dict[str, Any]:
        from ..lab.transfer import DriverLabTransfer
        name = self._guest_vm(vm_name)
        output = self.settings.evidence_root.expanduser().resolve()
        summary = {
            "package_id": package_id, "run_id": run_id,
            "output_root": str(output), "include_kernel_dump": bool(include_kernel_dump),
        }
        with self._approve("lab_collect", name, summary):
            return DriverLabTransfer(self).collect(
                name, package_id, run_id, output, include_kernel_dump=include_kernel_dump,
            )

    def _require_lab_profile(self, tool: str) -> None:
        """Executing a staged payload is a lab-profile decision, never a per-call one."""
        if self.settings.profile != "lab":
            raise PolicyError(
                f"{tool} runs a staged payload and is enabled only with HYPERV_MCP_PROFILE=lab. "
                "Without it, use lab_verify and run the package deliberately in the guest."
            )

    def lab_run(
        self,
        vm_name: str,
        package_id: str,
        run_id: str,
        expected_manifest_sha256: str,
        timeout_seconds: int = 600,
        checkpoint_before: bool = True,
    ) -> dict[str, Any]:
        from ..lab.run import checkpoint_name, run

        name = self._guest_vm(vm_name)
        self._require_lab_profile("lab_run")
        summary = {
            "package_id": package_id,
            "run_id": run_id,
            "manifest_sha256": expected_manifest_sha256,
            "checkpoint": checkpoint_name(run_id) if checkpoint_before else None,
            "timeout_seconds": timeout_seconds,
            "executes_staged_payload": True,
        }
        with self._approve("lab_run", name, summary):
            return self._tracked(
                "lab_run", name,
                lambda progress: run(
                    self, name,
                    package_id=package_id, run_id=run_id,
                    expected_manifest_sha256=expected_manifest_sha256,
                    timeout_seconds=timeout_seconds, checkpoint_before=checkpoint_before,
                    progress=progress,
                ),
            )

    def lab_cycle(
        self,
        vm_name: str,
        package_id: str,
        run_id: str,
        expected_manifest_sha256: str,
        timeout_seconds: int = 600,
        checkpoint_before: bool = True,
        revert_after: bool = False,
        include_kernel_dump: bool = True,
        debugger: str = "none",
        record: bool = False,
    ) -> dict[str, Any]:
        from ..lab.run import checkpoint_name, cycle

        name = self._guest_vm(vm_name)
        self._require_lab_profile("lab_cycle")
        summary = {
            "package_id": package_id,
            "run_id": run_id,
            "manifest_sha256": expected_manifest_sha256,
            "checkpoint": checkpoint_name(run_id) if checkpoint_before else None,
            "timeout_seconds": timeout_seconds,
            "revert_after": bool(revert_after),
            "debugger": debugger,
            "records_console": bool(record),
            "executes_staged_payload": True,
        }
        with self._approve("lab_cycle", name, summary):
            return self._tracked(
                "lab_cycle", name,
                lambda progress: cycle(
                    self, name,
                    package_id=package_id, run_id=run_id,
                    expected_manifest_sha256=expected_manifest_sha256,
                    timeout_seconds=timeout_seconds, checkpoint_before=checkpoint_before,
                    revert_after=revert_after, include_kernel_dump=include_kernel_dump,
                    debugger=debugger, record=record, progress=progress,
                ),
            )

    def lab_matrix(
        self,
        vm_names: list[str],
        package_id: str,
        run_id: str,
        expected_manifest_sha256: str,
        timeout_seconds: int = 600,
        checkpoint_before: bool = True,
        revert_after: bool = False,
        include_kernel_dump: bool = True,
        debugger: str = "none",
        record: bool = False,
    ) -> dict[str, Any]:
        """The same package through the same cycle on several VMs, compared.

        One approval covers the whole matrix, and its summary names every VM and
        the per-VM run ids, because a single click that runs a payload four times
        has to say so. The runs are sequential: each cycle owns the guest worker,
        a checkpoint and the console for its whole length.
        """
        from ..lab.matrix import matrix, normalise_vms, run_id_for
        from ..lab.run import checkpoint_name

        names = [self._guest_vm(value) for value in normalise_vms(vm_names)]
        self._require_lab_profile("lab_matrix")
        summary = {
            "package_id": package_id,
            "run_id": run_id,
            "vms": names,
            "run_ids": [run_id_for(run_id, value) for value in names],
            "manifest_sha256": expected_manifest_sha256,
            "checkpoint": checkpoint_name(run_id) if checkpoint_before else None,
            "timeout_seconds": timeout_seconds,
            "revert_after": bool(revert_after),
            "debugger": debugger,
            "records_console": bool(record),
            "executes_staged_payload": True,
        }
        with self._approve("lab_matrix", ",".join(names), summary):
            return self._tracked(
                "lab_matrix", ",".join(names),
                lambda progress: matrix(
                    self, names,
                    package_id=package_id, run_id=run_id,
                    expected_manifest_sha256=expected_manifest_sha256,
                    timeout_seconds=timeout_seconds, checkpoint_before=checkpoint_before,
                    revert_after=revert_after, include_kernel_dump=include_kernel_dump,
                    debugger=debugger, record=record, progress=progress,
                ),
            )

    def lab_verifier(
        self,
        vm_name: str,
        action: str,
        confirm_vm: str,
        drivers: list[str] | None = None,
        flags: str | None = None,
    ) -> dict[str, Any]:
        """Turn the verifier on, off, or back to what this tool replaced.

        The setting is a boot setting: it takes effect on the next restart, and
        this tool never restarts the guest.
        """
        from ..transport import guest_verifier
        name = self._guest_vm(vm_name)
        self._confirm(confirm_vm, name, "confirm_vm")
        # Validate before the approval card is drawn, so the summary is exactly
        # what the guest will be told.
        names = guest_verifier.normalise_drivers(drivers)
        mask = guest_verifier.normalise_flags(flags) if action == "enable" else None
        summary = {
            "action": action,
            "drivers": names,
            "flags": mask,
            "state_file": guest_verifier.GUEST_STATE_PATH,
            "reboots": False,
        }
        with self._approve("lab_verifier", name, summary):
            return guest_verifier.apply(self, name, action, drivers=names, flags=mask)

    def lab_driver_service(
        self,
        vm_name: str,
        action: str,
        service_name: str,
        confirm_vm: str,
        image_path: str = "",
        start_type: str = "demand",
    ) -> dict[str, Any]:
        """Install, start, stop or remove a test kernel-driver service.

        The image must already be staged under the guest's lab root, and nothing
        here bypasses signature enforcement: a driver the guest refuses to load
        is reported with the code it was refused with. Reading the state needs
        no confirmation; every other action needs ``confirm_vm``.
        """
        from ..transport import guest_driver_service
        name = self._guest_vm(vm_name)
        # Validate before the approval card is drawn, so the summary is exactly
        # what the guest will be told.
        options = guest_driver_service.request(action, service_name, image_path, start_type)
        if action == "status":
            return guest_driver_service.apply(self, name, **options)
        self._confirm(confirm_vm, name, "confirm_vm")
        with self._approve("lab_driver_service", name, {
            **options,
            "image_root": guest_driver_service.IMAGE_ROOT,
            "state_file": guest_driver_service.GUEST_STATE_PATH,
            "bypasses_signature_enforcement": False,
        }):
            return guest_driver_service.apply(self, name, **options)

    def lab_guest_configure(
        self,
        vm_name: str,
        confirm_vm: str,
        *,
        testsigning: bool | None = None,
        kernel_debug: bool | None = None,
        crash_dump: str | bool | None = None,
        disable_hvci: bool | None = None,
        secure_boot_off: bool = False,
        restart: bool = False,
    ) -> dict[str, Any]:
        """Prepare the guest for driver work; every change is recorded and revertible."""
        from ..transport import guest_config

        name = self._guest_vm(vm_name)
        self._confirm(confirm_vm, name, "confirm_vm")
        for label, value in (
            ("testsigning", testsigning), ("kernel_debug", kernel_debug),
            ("disable_hvci", disable_hvci),
        ):
            if value is not None and not isinstance(value, bool):
                raise ValidationError(f"{label} must be true, false, or omitted.")
        if crash_dump is not None and crash_dump is not False and crash_dump not in {
            "kernel", "complete", "none"
        }:
            raise ValidationError("crash_dump must be kernel, complete, none, false, or omitted.")
        for label, value in (("secure_boot_off", secure_boot_off), ("restart", restart)):
            if not isinstance(value, bool):
                raise ValidationError(f"{label} must be a boolean value.")

        firmware = self._run("vm_firmware", "vm_firmware", {"vm": name}, vm_name=name)
        secure_boot = str(firmware.get("secure_boot") or "")
        if secure_boot_off and secure_boot.casefold() == "on":
            state = str(self.get_vm(name)["vm"].get("state", ""))
            if state.casefold() != "off":
                raise PolicyError(
                    "Secure Boot can only be turned off while the VM is Off; it is "
                    f"{state or 'unknown'}. Power it off deliberately, call this tool again "
                    "with secure_boot_off=true, then start it and call it again to configure "
                    "the guest. This tool never changes power state."
                )
            with self._approve("vm_secure_boot", name, {
                "secure_boot": "Off", "generation": firmware.get("generation"),
            }):
                changed = self._run(
                    "vm_secure_boot", "vm_secure_boot", {"vm": name, "enable": False},
                    vm_name=name,
                )
            return {
                "vm": name,
                "firmware": firmware,
                "secure_boot": changed.get("secure_boot"),
                "guest": None,
                "reboot_required": False,
                "next_steps": [
                    "Start the VM deliberately.",
                    "Call lab_guest_configure again with the VM Running to apply the guest settings.",
                ],
            }
        if secure_boot.casefold() == "on" and (testsigning or kernel_debug):
            raise PolicyError(
                "Secure Boot is On, so test signing and kernel debugging would be ignored. "
                "Power the VM off and call this tool with secure_boot_off=true, or disable "
                "Secure Boot deliberately in Hyper-V first."
            )

        summary = {
            "testsigning": testsigning,
            "kernel_debug": kernel_debug,
            "crash_dump": crash_dump,
            "disable_hvci": disable_hvci,
            "state_file": guest_config.GUEST_STATE_PATH,
        }
        with self._approve("lab_guest_configure", name, summary):
            applied = guest_config.lab_apply(
                self, name,
                testsigning=testsigning, kernel_debug=kernel_debug,
                crash_dump=crash_dump, hvci=disable_hvci,
            )
        result: dict[str, Any] = {
            "vm": name,
            "firmware": firmware,
            "secure_boot": firmware.get("secure_boot"),
            "guest": applied,
            "reboot_required": bool(applied.get("changed")),
            "restart": None,
        }
        if restart:
            if not self.settings.allows(name, "power"):
                raise PolicyError(
                    self.settings.refusal(name, "power"),
                    fix={"operator": self._raise_to(name, "power")},
                )
            result["restart"] = self._restart_and_verify(name)
        return result
