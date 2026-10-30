"""The frozen launcher: the same ``cli:main`` the console script runs.

PyInstaller executes this file as ``__main__``, where a relative import has no
package to resolve against, so the entry point is imported absolutely and its
return value becomes the exit code. Nothing else changes: with a pipe on stdin
it is the server, with a console or no stdin at all it is the window.
"""

import sys

from hyperv_console_mcp.cli import main

if __name__ == "__main__":
    sys.exit(main())
