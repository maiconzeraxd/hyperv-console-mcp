"""The WebView2 window: a responsive page over the same policy-gated service.

``run`` opens it and reports whether it could; the tkinter window in
``hyperv_console_mcp.dashboard`` stays as the fallback for a host without the
WebView2 runtime, and the two are clients of the same service, so switching
between them loses no session and no event.
"""

from __future__ import annotations

__all__ = ["api", "window"]
