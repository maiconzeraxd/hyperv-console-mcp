"""The frozen setup routine: the same ``setup:main`` as ``hyperv-setup``.

The installer runs this with ``--no-shortcut`` (it makes its own) and its
uninstaller runs it with ``--uninstall``, so one routine answers "what does an
install do on this machine" for the ``.exe``, the script and the package.
"""

import sys

from hyperv_console_mcp.setup import main

if __name__ == "__main__":
    sys.exit(main())
