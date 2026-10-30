"""The frozen host: the same ``service.host:main`` as ``hyperv-service``.

A client that finds no host starts this program, so it has to exist beside the
launcher — a client that could not start its own host would open the window and
sit there with nothing answering.
"""

import sys

from hyperv_console_mcp.service.host import main

if __name__ == "__main__":
    sys.exit(main())
