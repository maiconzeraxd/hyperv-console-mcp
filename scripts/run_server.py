"""Run directly from a source checkout with an isolated Python interpreter."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hyperv_console_mcp.server import main

if __name__ == "__main__":
    main()
