"""The page, assembled: one HTML file with every asset inline.

The window passes its HTML to WebView2 as a string, so a relative `<script
src="…">` has nothing to resolve against — and `--serve` should not have to
publish a second file to make the terminals work. Both modes therefore get the
page from here, where the vendored scripts are inlined at read time:

- `index.html` is the page as a person maintains it, with two markers where the
  vendored code goes.
- `vendor/` holds the third-party files exactly as published, with their
  licence, so what ships is auditable and the version is visible in one place.

Nothing here interprets the page. It replaces two markers and returns the text,
and a missing vendor file is an error rather than a page that silently loads
without terminals.
"""

from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).with_name("assets")
VENDOR = ASSETS / "vendor"
# The markers live inside the page's own <style> and before its <script>, so the
# assembled file has no new structure — only more text where the marker was.
CSS_MARKER = "/*__VENDOR_CSS__*/"
JS_MARKER = "/*__VENDOR_JS__*/"
VENDOR_FILES = {
    CSS_MARKER: VENDOR / "xterm.css",
    JS_MARKER: VENDOR / "xterm.js",
}


def page_text() -> str:
    """The page with the vendored CSS and script inlined."""
    text = (ASSETS / "index.html").read_text(encoding="utf-8")
    for marker, path in VENDOR_FILES.items():
        if marker not in text:
            raise RuntimeError(
                f"{marker} is missing from index.html; the page would load without its terminals."
            )
        try:
            text = text.replace(marker, path.read_text(encoding="utf-8"), 1)
        except OSError as exc:
            raise RuntimeError(f"{path.name} is missing from the package: {exc}") from exc
    return text


__all__ = ["ASSETS", "CSS_MARKER", "JS_MARKER", "VENDOR", "page_text"]
