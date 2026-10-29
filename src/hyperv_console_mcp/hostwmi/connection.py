"""The Hyper-V WMI provider, through the only module that imports win32com.

Every object crosses this boundary as plain data: a query returns a list of
property dictionaries, and each carries ``__PATH`` so a caller can invoke a
method on it. That keeps the mapping code in Python — where it can be tested
without a hypervisor — and confines COM to one file.
"""

from __future__ import annotations

from typing import Any, Protocol

HYPERV_NAMESPACE = r"root\virtualization\v2"
CIMV2_NAMESPACE = r"root\cimv2"

# WMI impersonation: act as the caller, so Hyper-V refuses a VM the account may
# not manage instead of the call succeeding under a wider identity.
_IMPERSONATE = 3


class WmiFailure(RuntimeError):
    """A WMI call failed. ``code`` keeps the MCP error taxonomy."""

    def __init__(
        self, message: str, *, code: str = "backend_error", fix: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.fix = fix


class Connection(Protocol):
    """What the mapping code needs; a fake implements the same three calls."""

    def query(self, wql: str, *, namespace: str = HYPERV_NAMESPACE) -> list[dict[str, Any]]:
        ...

    def get(self, path: str, *, namespace: str = HYPERV_NAMESPACE) -> dict[str, Any]:
        ...

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        ...


class ComConnection:
    """One COM connection, with a service object per namespace.

    A worker thread calls in from its own apartment, so the connection is
    created per thread and never shared between them.
    """

    def __init__(self, computer: str = ".") -> None:
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise WmiFailure(
                "pywin32 is not installed, so host operations cannot use WMI. "
                "Install the 'wmi' extra or set HYPERV_MCP_HOST_BACKEND=powershell.",
                code="wmi_unavailable",
            ) from exc
        self._computer = computer
        self._services: dict[str, Any] = {}
        try:
            pythoncom.CoInitialize()
            self._locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
        except Exception as exc:  # pywintypes.com_error and friends
            raise WmiFailure(
                f"Could not open the WMI locator: {exc}", code="wmi_unavailable"
            ) from exc

    def _service(self, namespace: str) -> Any:
        service = self._services.get(namespace)
        if service is None:
            try:
                service = self._locator.ConnectServer(self._computer, namespace)
                service.Security_.ImpersonationLevel = _IMPERSONATE
            except Exception as exc:
                raise WmiFailure(
                    f"Could not connect to {namespace}: {exc}", code="wmi_unavailable"
                ) from exc
            self._services[namespace] = service
        return service

    def query(self, wql: str, *, namespace: str = HYPERV_NAMESPACE) -> list[dict[str, Any]]:
        try:
            collection = self._service(namespace).ExecQuery(wql)
            return [_as_dict(collection.ItemIndex(index)) for index in range(collection.Count)]
        except WmiFailure:
            raise
        except Exception as exc:
            raise WmiFailure(f"WMI query failed: {exc}") from exc

    def get(self, path: str, *, namespace: str = HYPERV_NAMESPACE) -> dict[str, Any]:
        try:
            return _as_dict(self._service(namespace).Get(path))
        except WmiFailure:
            raise
        except Exception as exc:
            raise WmiFailure(f"Could not read {path}: {exc}") from exc

    def call(self, path: str, method: str, **parameters: Any) -> dict[str, Any]:
        service = self._service(HYPERV_NAMESPACE)
        try:
            target = service.Get(path)
            in_parameters = target.Methods_(method).InParameters
            if in_parameters is None:
                # A method with no in-parameters — InjectNonMaskableInterrupt is
                # one — answers None here, and ExecMethod takes None for it.
                if parameters:
                    raise WmiFailure(
                        f"{method} takes no parameters, but {sorted(parameters)} were given.",
                        code="invalid_argument",
                    )
                return _as_dict(service.ExecMethod(path, method, None))
            instance = in_parameters.SpawnInstance_()
            for name, value in parameters.items():
                _set_property(instance, name, value)
            return _as_dict(service.ExecMethod(path, method, instance))
        except WmiFailure:
            raise
        except Exception as exc:
            raise WmiFailure(f"WMI method {method} failed: {exc}") from exc


def _set_property(obj: Any, name: str, value: Any) -> None:
    """Write one property of a WMI instance through its property set.

    ``setattr`` is not an option here: pywin32's gen_py wrapper resolves
    attribute writes against the type library, and a parameter object from
    ``SpawnInstance_()`` carries no such entries, so it raises ``KeyError``
    before the value ever reaches the provider.
    """
    obj.Properties_.Item(name).Value = value


def _as_dict(obj: Any) -> dict[str, Any]:
    """Flatten a Wbem object into a dict, keeping its path and class.

    ``__CLASS`` matters because an association query answers with objects of
    several classes at once, and the mapping code needs to tell a processor
    setting from a disk one.
    """
    values: dict[str, Any] = {}
    for prop in obj.Properties_:
        name = str(prop.Name)
        if not name.startswith("__"):
            values[name] = prop.Value
    path = getattr(obj, "Path_", None)
    if path is not None:
        if path.Path:
            values["__PATH"] = path.Path
        if path.Class:
            values["__CLASS"] = path.Class
    return values


def wql_literal(value: str) -> str:
    """Quote a string for WQL: backslash and single quote both escape."""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"
