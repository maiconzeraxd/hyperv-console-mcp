"""ISO media on an existing DVD drive."""

from __future__ import annotations

from typing import Any

from ..errors import ValidationError
from ._base import ServiceBase, _confined_path, _integer


class MediaService(ServiceBase):
    def media_list(self, vm_name: str) -> dict[str, Any]:
        name = self._vm(vm_name)
        return self._run("media_list", "media_list", {"vm": name}, vm_name=name)

    def media_mount(
        self,
        vm_name: str,
        iso_path: str,
        controller_location: int,
        confirm_vm: str,
        boot_first: bool = False,
        controller_number: int = 0,
    ) -> dict[str, Any]:
        vm = self._write(vm_name, "media")
        self._confirm(confirm_vm, vm, "confirm_vm")
        path = _confined_path(iso_path, "iso_path", self.settings.iso_root, ".iso")
        if not isinstance(boot_first, bool):
            raise ValidationError("boot_first must be a boolean value.")
        number = _integer(controller_number, "controller_number", 0, 1)
        location = _integer(controller_location, "controller_location", 0, 63)
        summary = {
            "iso_path": path, "boot_first": boot_first,
            "controller_number": number, "controller_location": location,
        }
        with self._approve("media_mount", vm, summary):
            return self._run(
                "media_mount", "media_mount",
                {"vm": vm, "path": path, "controller_number": number,
                 "controller_location": location, "boot_first": boot_first},
                vm_name=vm,
            )

    def media_eject(
        self, vm_name: str, controller_location: int, confirm_vm: str, controller_number: int = 0
    ) -> dict[str, Any]:
        vm = self._write(vm_name, "media")
        self._confirm(confirm_vm, vm, "confirm_vm")
        number = _integer(controller_number, "controller_number", 0, 1)
        location = _integer(controller_location, "controller_location", 0, 63)
        with self._approve("media_eject", vm, {
            "controller_number": number, "controller_location": location,
        }):
            return self._run(
                "media_eject", "media_eject",
                {"vm": vm, "controller_number": number, "controller_location": location},
                vm_name=vm,
            )
