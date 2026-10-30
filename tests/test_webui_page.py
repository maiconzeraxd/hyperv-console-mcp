"""Properties of the page itself: two palettes, a visible focus, a small window.

These are checks on the stylesheet and the markup the script builds, read as
text. They cannot tell whether the result looks right — that is what looking at
it is for — but they do catch the two ways this kind of page rots silently: a
colour written straight into a rule, which the light theme then cannot reach,
and a control that only a pointer can operate.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PAGE = (
    Path(__file__).resolve().parents[1]
    / "src" / "hyperv_console_mcp" / "webui" / "assets" / "index.html"
)

# The guest's own screen is black in both themes: it is a picture of a display,
# not a surface of this window.
ALLOWED_LITERALS = {"#000", "#000000"}

PALETTE = re.compile(r"--([a-z-]+):\s*(#[0-9a-fA-F]{3,8})")


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _blocks() -> dict[str, str]:
    """The dark root block and the light override, as text."""
    text = _page()
    dark = re.search(r"\n  :root \{(.*?)\n  \}", text, re.S)
    light = re.search(r'\n  :root\[data-theme="light"\] \{(.*?)\n  \}', text, re.S)
    assert dark and light, "the two palette blocks are the contract"
    return {"dark": dark.group(1), "light": light.group(1)}


def _without_palettes(text: str) -> str:
    for block in _blocks().values():
        text = text.replace(block, "")
    return text


def test_the_two_palettes_define_the_same_names() -> None:
    """A name the light theme forgets is a colour that stays dark on a light page."""
    blocks = _blocks()
    dark = {name for name, _ in PALETTE.findall(blocks["dark"])}
    light = {name for name, _ in PALETTE.findall(blocks["light"])}
    assert sorted(dark - light) == []
    assert sorted(light - dark) == []


def test_no_colour_is_written_outside_the_palettes() -> None:
    """The guard that keeps the light theme reachable as the page grows."""
    text = _without_palettes(_page())
    literals = set(re.findall(r"#[0-9a-fA-F]{3,8}\b", text))
    assert sorted(literals - ALLOWED_LITERALS) == []


def test_the_light_theme_sets_its_own_color_scheme() -> None:
    """Without this the scrollbars and form controls stay dark on a light page."""
    assert 'color-scheme: light' in _blocks()["light"]
    assert "color-scheme: dark" in _blocks()["dark"]


def test_the_page_follows_the_system_until_it_is_told_otherwise() -> None:
    text = _page()
    assert "prefers-color-scheme: light" in text
    assert 'localStorage.getItem(THEME_KEY)' in text
    # The choice is the window's own and never reaches the service.
    assert "set_level" not in text.split("THEME_KEY")[0].split("const THEME_KEY")[-1]


def test_keyboard_focus_is_visible() -> None:
    text = _page()
    assert ":focus-visible { outline:" in text
    # The console removes the outline on the image itself, so it has to put one back.
    assert ".screen:focus-visible" in text


def test_a_small_window_is_a_layout_and_not_a_scrollbar() -> None:
    assert "@media (max-width: 1100px)" in _page()


def _clickable_non_buttons() -> list[str]:
    """`el("div", {… onclick …})` and friends: reachable by pointer only.

    Scans the attributes object of each `el(tag, {…})` for an `onclick`, and
    reports the tags that are not interactive elements. An element that declares
    a role, a tab stop and a key handler is not one of them: it was promoted to a
    control on purpose, and the three together are what make that true.
    """
    text = _page()
    found: list[str] = []
    for match in re.finditer(r'el\("([a-z]+)", \{', text):
        tag = match.group(1)
        depth = 0
        start = match.end() - 1
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
        attributes = text[start:index]
        if "onclick" not in attributes:
            continue
        if tag in {"button", "a", "input", "select", "textarea"}:
            continue
        promoted = all(key in attributes for key in ("role:", "tabindex:", "onkeydown:"))
        if not promoted:
            found.append(tag)
    return found


def test_every_control_the_script_builds_can_be_operated_by_keyboard() -> None:
    assert _clickable_non_buttons() == []


def test_the_workspace_tab_opens_and_closes_from_the_keyboard() -> None:
    text = _page()
    assert 'class: "tab-open"' in text
    assert '"aria-label": "Close "' in text
    assert '"aria-current"' in text


def test_the_page_announces_what_changes_on_its_own() -> None:
    """The connection light and the result drawer change without a click."""
    text = _page()
    assert 'role="status" aria-live="polite"' in text
    assert 'role="status" aria-live="polite"></pre>' in text


def test_a_dragged_file_is_the_same_copy_the_button_makes() -> None:
    """A drop must not grow a second transfer path.

    Dragging is a shortcut for the row's own "To guest →"/"To host →" button, so
    it has to end at ``copyEntry`` rather than at a transfer of its own — and the
    button stays, because a drag is the one gesture a keyboard cannot make.
    """
    text = _page()
    assert 'draggable: "true"' in text
    assert 'addEventListener("drop"' in text
    assert "await copyEntry(carried.side, carried.entry" in text
    # One implementation of each direction, reached by both gestures.
    assert text.count('call("guest_put_file"') == 1
    assert text.count('call("guest_get_file"') == 1
    # The panel a file would land in says so, in the palette's own colour.
    assert ".card.dropping { outline: 2px dashed var(--accent)" in text


def test_the_guest_bcd_form_offers_only_the_dump_modes_the_service_accepts() -> None:
    """A select the service refuses is a control that cannot do what it says.

    The page listed ``small`` and ``full``; ``lab_guest_configure`` validates
    against ``kernel``, ``complete`` and ``none``, so two of its five choices
    answered with a validation error. The accepted set is read from the service
    rather than repeated here, so adding a mode on one side fails until the
    other catches up.
    """
    from hyperv_console_mcp.service.lab import CRASH_DUMP_MODES

    match = re.search(r'\["", ([^\]]*)\]\.map', _page())
    assert match, "the crash-dump select is built from one array"
    offered = {
        value.strip().strip('"')
        for value in match.group(1).split(",")
        if value.strip()
    }
    assert offered <= set(CRASH_DUMP_MODES), (
        "the page offers "
        f"{sorted(offered - set(CRASH_DUMP_MODES))}, which lab_guest_configure refuses"
    )

def test_the_page_speaks_http_when_it_was_served_with_a_token() -> None:
    """`--serve` is a transport, not a second page.

    The token meta tag is empty in the WebView2 window and filled by the
    listener. With it, the page must reach the same method names over HTTP and
    boot from the document — `pywebviewready` never fires in a browser, so a
    page that only listened for it would open blank.
    """
    text = _page()
    assert 'name="hyperv-token" content=""' in text
    assert "X-HyperV-Token" in text
    assert 'fetch("/api"' in text
    # Both doors: the bridge in the window, the document when served.
    assert 'addEventListener("pywebviewready", boot)' in text
    assert 'DOMContentLoaded", boot' in text

def test_the_log_dock_offers_only_the_sources_the_service_names() -> None:
    """A source the service refuses is a filter that can only answer 400.

    The list is read from the service rather than repeated in the page, so
    adding a source on one side fails until the other catches up — the same
    shape as the crash-dump select.
    """
    from hyperv_console_mcp.logs import SOURCES

    text = _page()
    match = re.search(r'id="log-source".*?</select>', text, re.S)
    assert match, "the dock's source filter is a select"
    offered = {
        value for value in re.findall(r'<option value="([a-z]+)">', match.group(0))
    }
    assert offered == set(SOURCES)


def test_the_log_dock_can_be_paused_searched_and_grouped() -> None:
    """The dock is a window on the stream: it can stop, narrow and group.

    Grouping is what makes an operation read as one run: the correlation id is
    drawn on the first line of a group and left blank on the rest.
    """
    text = _page()
    assert 'id="log-pause"' in text and 'aria-pressed="false"' in text
    assert 'id="log-search"' in text
    assert "dock.lastCorrelation" in text
    assert 'id="log-body"' in text
    # A bounded window, not a growing store: the oldest line leaves first.
    assert "body.childElementCount > 500" in text

def test_the_page_ships_its_terminals_inline() -> None:
    """xterm is inlined when the page is read, not linked.

    The window hands WebView2 a string, so a relative `<script src>` would have
    nothing to resolve against, and `--serve` publishes one page rather than a
    directory. The markers are the only seam: the assembled page has neither.
    """
    from hyperv_console_mcp.webui import page as assembly

    text = assembly.page_text()

    assert assembly.CSS_MARKER not in text
    assert assembly.JS_MARKER not in text
    assert "Terminal" in text
    assert len(text) > (assembly.ASSETS / "index.html").stat().st_size


def test_a_missing_vendor_file_is_an_error_not_a_page_without_terminals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyperv_console_mcp.webui import page as assembly

    monkeypatch.setitem(assembly.VENDOR_FILES, assembly.JS_MARKER, tmp_path / "absent.js")

    with pytest.raises(RuntimeError, match="absent.js"):
        assembly.page_text()

def test_the_guest_terminal_is_a_terminal_and_not_a_styled_box() -> None:
    """xterm over `guest_exec`, painted from the palette rather than restated.

    A `<pre>` would look like a terminal and behave like a log: no cursor, no
    line editing, no scrollback. The widget is xterm, and its colours are read
    from the page's own tokens so the light theme keeps working and no colour is
    written twice.
    """
    text = _page()
    assert "new window.Terminal(" in text
    assert 'api().call("guest_exec"' in text
    assert 'id: "guest-term"' in text
    assert "terminalTheme" in text
    assert 'css("--bg")' in text
    # One command at a time: the guest transport is serial, so the input is
    # held while a call is in flight rather than queued behind it.
    assert "if (term.busy) return;" in text

def test_the_permission_matrix_offers_exactly_the_capabilities_the_service_knows() -> None:
    """A checkbox for a capability the policy never names is a control that lies.

    The page keeps its own list so it can draw the grid without a round trip;
    the list is compared against ``access.CAPABILITIES`` here so adding one on
    either side fails until the other catches up.
    """
    from hyperv_console_mcp.access import CAPABILITIES

    match = re.search(r"const MATRIX = \[([^\]]*)\];", _page())
    assert match, "the matrix is built from one array"
    offered = {value.strip().strip('"') for value in match.group(1).split(",") if value.strip()}
    assert offered == set(CAPABILITIES), (
        "the matrix and the policy disagree: "
        f"only in the page {sorted(offered - set(CAPABILITIES))}, "
        f"only in the policy {sorted(set(CAPABILITIES) - offered)}"
    )


def test_applying_a_level_sends_the_capabilities_the_operator_kept() -> None:
    """The grid is worthless if Apply posts only the level."""
    page = _page()
    assert "const kept = level ? keptCaps(level) : null;" in page
    assert "await api().set_level(level, kept)" in page
    # And an unchanged grid is not a narrowing: it says nothing.
    assert "return kept.length === allowed.length ? null : kept;" in page


def test_the_matrix_starts_from_the_saved_level_after_every_render() -> None:
    """The grid is drawn into a node the render just replaced."""
    page = _page()
    assert 'if (state.page === "overview") {' in page
    assert "renderCaps(select.value);" in page

def test_an_empty_matrix_is_refused_instead_of_allowing_nothing() -> None:
    """A page that can silently leave a VM allowed nothing is a trap.

    The file would then say ``capabilities: []``, which reads as "listed, and
    permitted nothing" — and the operator's next question is why the AI cannot
    reach the VM it can see.
    """
    page = _page()
    assert "if (level && kept && !kept.length) {" in page
    assert "Remove the level instead of allowing nothing." in page

