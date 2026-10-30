"""The build files agree with the code, or a release ships the wrong program.

None of this runs PyInstaller, Inno Setup or a runner. What is checked is the
part that goes wrong quietly: the spec freezing four programs whose names the
code also uses, every console script either shipped or deliberately left out,
the installer running the routine's own flags rather than a second installer,
and winget's uninstall key matching Inno's ``AppId``.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

from hyperv_console_mcp import __version__, frozen

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "hyperv-console-mcp.spec"
INSTALLER = ROOT / "packaging" / "installer.iss"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
WINGET = ROOT / "packaging" / "winget"
ENTRYPOINTS = ROOT / "packaging" / "entrypoints"

# Shipped: the stub that stands in for the console script, by entry point.
SHIPPED = {
    "hyperv-console-mcp": ("launcher.py", "hyperv_console_mcp.cli"),
    "hyperv-setup": ("setup_cli.py", "hyperv_console_mcp.setup"),
    "hyperv-service": ("service_cli.py", "hyperv_console_mcp.service.host"),
    "hyperv-access": ("access_cli.py", "hyperv_console_mcp.access_cli"),
}
# Left out, and why. A frozen install is for the window and the operator tools;
# the lab runs from an interpreter, `hyperv-dashboard` is the launcher's other
# name, the tray needs an optional extra the bundle does not carry, and
# `hyperv-install` only writes the config `hyperv-setup` already writes.
NOT_SHIPPED = {
    "hyperv-lab": "the lab runs from an interpreter, where its packages already are",
    "hyperv-dashboard": "the launcher opens the same window",
    "hyperv-tray": "it needs the optional pystray extra",
    "hyperv-install": "hyperv-setup writes the same client entries and shortcut",
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _console_scripts() -> dict[str, str]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["scripts"]


def test_the_spec_parses_as_python() -> None:
    """A spec is code: a syntax error there is a release that cannot build."""
    ast.parse(_text(SPEC))


def test_the_spec_freezes_the_programs_the_code_names() -> None:
    text = _text(SPEC)
    assert "from hyperv_console_mcp.frozen import ACCESS, HOST, LAUNCHER, SETUP" in text
    calls = re.findall(
        r'program\("(\w+)", (\w+), console=(True|False)(?:, uac_admin=(True|False))?\)', text
    )
    assert sorted(name for _, name, _, _ in calls) == ["ACCESS", "HOST", "LAUNCHER", "SETUP"]
    # The launcher is the only windowed program: a console would flash on every
    # double-click, and the three operator tools print their answers.
    assert [name for _, name, console, _ in calls if console == "False"] == ["LAUNCHER"]


def test_the_spec_launches_the_window_as_administrator() -> None:
    """The launcher must always run elevated, which the manifest asks for."""
    text = _text(SPEC)
    assert "uac_admin=True" in text
    assert 'program("launcher", LAUNCHER, console=False, uac_admin=True)' in text


def test_every_console_script_is_shipped_or_named_as_left_out() -> None:
    scripts = _console_scripts()
    assert set(scripts) == set(SHIPPED) | set(NOT_SHIPPED)


@pytest.mark.parametrize(("script", "stub_and_module"), sorted(SHIPPED.items()))
def test_a_shipped_stub_runs_its_console_script(script: str, stub_and_module: tuple[str, str]) -> None:
    stub, module = stub_and_module
    assert _console_scripts()[script] == f"{module}:main"
    body = _text(ENTRYPOINTS / stub)
    assert f"from {module} import main" in body
    assert "sys.exit(main())" in body


def test_the_installer_installs_the_programs_the_spec_builds() -> None:
    text = _text(INSTALLER)
    assert f'#define Launcher "{frozen.LAUNCHER}"' in text
    assert f'#define SetupExe "{frozen.SETUP}"' in text


def test_installing_asks_for_no_elevation() -> None:
    """The first screen after a download should be the window, not a UAC prompt."""
    text = _text(INSTALLER)
    assert "PrivilegesRequired=lowest" in text
    assert "DefaultDirName={localappdata}" in text


def test_the_installer_runs_the_routine_rather_than_a_second_installer() -> None:
    text = _text(INSTALLER)
    assert 'Parameters: "--no-shortcut"' in text
    assert "[UninstallRun]" in text
    assert 'Parameters: "--uninstall"' in text


def test_the_release_workflow_builds_both_artifacts() -> None:
    text = _text(WORKFLOW)
    assert "packaging/hyperv-console-mcp.spec" in text
    assert "packaging\\installer.iss" in text
    assert "dist/installer/*.exe" in text


def test_the_release_workflow_only_ships_tags_and_says_when_unsigned() -> None:
    text = _text(WORKFLOW)
    assert "tags: ['v*']" in text
    assert "if: env.SIGNING_CERT != ''" in text
    assert "if: env.SIGNING_CERT == ''" in text
    assert "SmartScreen" in text


def test_the_winget_uninstall_key_is_the_installer_s_app_id() -> None:
    """If the AppId changes, an upgrade through winget leaves the old program behind."""
    app_id = re.search(r"AppId=\{\{([^}]+)\}", _text(INSTALLER))
    assert app_id is not None
    product_code = "{" + app_id.group(1) + "}_is1"
    assert product_code in _text(WINGET / "maiconzeraxd.HyperVConsoleMCP.installer.yaml")


def test_the_winget_manifests_describe_one_package() -> None:
    files = sorted(WINGET.glob("maiconzeraxd.HyperVConsoleMCP*.yaml"))
    assert len(files) == 3
    for path in files:
        text = _text(path)
        assert "PackageIdentifier: maiconzeraxd.HyperVConsoleMCP" in text
        assert "ManifestVersion: 1.6.0" in text
        assert f"PackageVersion: {__version__}" in text


def test_the_winget_hash_is_a_hash_or_the_placeholder() -> None:
    """The placeholder is legitimate only until the tag has built an installer.

    A wrong hash is worse than the zeros: winget accepts the manifest, then
    every `winget install` refuses the download with a checksum mismatch. So the
    value has to be either the all-zero marker that says "not filled in yet" or
    a real SHA-256 - never a truncated or mistyped one.
    """
    text = _text(WINGET / "maiconzeraxd.HyperVConsoleMCP.installer.yaml")
    match = re.search(r"InstallerSha256:\s*'([0-9a-fA-F]+)'", text)
    assert match is not None, "the installer manifest names no InstallerSha256"
    value = match.group(1)
    assert value == "0" * 64 or re.fullmatch(r"[0-9a-f]{64}", value), value

def test_the_wheel_carries_the_vendored_terminal() -> None:
    """A page whose terminal is inlined needs the vendor files in the package.

    `webui/page.py` raises when one is missing, so a wheel without them would
    install cleanly and fail at the first window — this is the check that keeps
    that from shipping.
    """
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as handle:
        package_data = tomllib.load(handle)["tool"]["setuptools"]["package-data"]
    assert "webui/assets/vendor/*" in package_data["hyperv_console_mcp"]
    vendor = root / "src" / "hyperv_console_mcp" / "webui" / "assets" / "vendor"
    assert (vendor / "xterm.js").is_file()
    assert (vendor / "xterm.css").is_file()
    assert (vendor / "NOTICE.md").is_file()

