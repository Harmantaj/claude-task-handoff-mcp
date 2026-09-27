"""Claude Desktop extension entry point: run the bundled, dependency-free server."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from task_handoff.server import main  # noqa: E402

if __name__ == "__main__":
    main()
