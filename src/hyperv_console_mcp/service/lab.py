"""The hash-verified driver lab: init, stage, run, cycle and evidence."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..errors import PolicyError, ValidationError
from ._base import ServiceBase, _text


class LabService(ServiceBase):
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
            return run(
                self, name,
                package_id=package_id, run_id=run_id,
                expected_manifest_sha256=expected_manifest_sha256,
                timeout_seconds=timeout_seconds, checkpoint_before=checkpoint_before,
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
            "executes_staged_payload": True,
        }
        with self._approve("lab_cycle", name, summary):
            return cycle(
                self, name,
                package_id=package_id, run_id=run_id,
                expected_manifest_sha256=expected_manifest_sha256,
                timeout_seconds=timeout_seconds, checkpoint_before=checkpoint_before,
                revert_after=revert_after, include_kernel_dump=include_kernel_dump,
                debugger=debugger,
            )

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
            if not self.settings.allow_power:
                raise PolicyError("Restarting the guest requires HYPERV_MCP_ALLOW_POWER.")
            result["restart"] = self._restart_and_verify(name)
        return result
