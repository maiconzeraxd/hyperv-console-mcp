"""The frozen access tool: the same ``access_cli:main`` as ``hyperv-access``.

It ships because the refusal the AI and the window both print names it —
``hyperv-access set "VM" control|lab`` — and advice that points at a program the
operator does not have is worse than no advice.
"""

import sys

from hyperv_console_mcp.access_cli import main

if __name__ == "__main__":
    sys.exit(main())
