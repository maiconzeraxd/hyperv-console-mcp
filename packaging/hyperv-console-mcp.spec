# -*- mode: python ; coding: utf-8 -*-
"""The frozen build: one folder, four programs, no interpreter needed.

    pyinstaller packaging/hyperv-console-mcp.spec --noconfirm

Run after ``pip install -e ".[ui]"``, which is what the release workflow does.
The program names come from :mod:`hyperv_console_mcp.frozen`, so this file, the
Inno Setup script and the three places in the code that name a program cannot
disagree about what a file is called.

One folder rather than one file: a single-file build unpacks itself into a
temporary directory on every start, which is slower, leaves copies behind and
is the shape antivirus heuristics dislike most. The installer wraps the folder,
so the operator still downloads one thing.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).resolve().parent
# The spec runs before `Analysis` and so before `pathex` exists: the package has
# to be importable here for the program names and for the icon.
sys.path.insert(0, str(ROOT / "src"))

from hyperv_console_mcp.frozen import ACCESS, HOST, LAUNCHER, SETUP  # noqa: E402

ENTRYPOINTS = ROOT / "packaging" / "entrypoints"
ICON = ROOT / "dist" / "icon.ico"

# The same glyph the tray shows, drawn at build time so no binary is committed.
if not ICON.is_file():
    from hyperv_console_mcp.tray import write_icon

    write_icon(ICON)

# The PowerShell bridge scripts and the single-file page: the package needs
# them at runtime and none of them is an import.
datas = collect_data_files("hyperv_console_mcp")

# pywebview imports its Windows backend by name at runtime. Both are optional —
# without pywebview the window falls back to tkinter — so a missing hook costs
# the WebView2 window, not the build.
hiddenimports = [
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
]

# One analysis for all four programs: they share an import graph, and one folder
# can only hold one set of binaries anyway.
analysis = Analysis(
    [
        str(ENTRYPOINTS / "launcher.py"),
        str(ENTRYPOINTS / "setup_cli.py"),
        str(ENTRYPOINTS / "service_cli.py"),
        str(ENTRYPOINTS / "access_cli.py"),
    ],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "tests"],
)
pyz = PYZ(analysis.pure)

PROGRAMS = ("launcher", "setup_cli", "service_cli", "access_cli")


def scripts_for(name: str):
    """The runtime hooks plus this one program — not the other three.

    All four programs share the analysis, so its script table carries all four;
    handing that table to an ``EXE`` would put four entry points in one bundle.
    The hooks stay because they set the frozen environment up before the
    program runs.
    """
    return [item for item in analysis.scripts if item[0] not in PROGRAMS or item[0] == name]


def program(script: str, name: str, console: bool):
    """One executable from its own script, sharing what ``COLLECT`` gathers."""
    return EXE(
        pyz,
        scripts_for(script),
        [],
        exclude_binaries=True,
        name=Path(name).stem,
        console=console,
        icon=str(ICON),
        upx=False,
    )


# The launcher is windowed: double-clicking it must not open a console, and a
# client that hands it a pipe still gets the server because stdin is a pipe and
# not a console. The three operator tools keep their console: they print.
COLLECT(
    program("launcher", LAUNCHER, console=False),
    program("setup_cli", SETUP, console=True),
    program("service_cli", HOST, console=True),
    program("access_cli", ACCESS, console=True),
    analysis.binaries,
    analysis.datas,
    name="HyperVConsoleMCP",
    upx=False,
)
