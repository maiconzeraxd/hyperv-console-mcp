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
