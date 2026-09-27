"""Run the Task Handoff MCP server straight from a git clone (no install, no dependencies).

    claude mcp add --scope user task-handoff -- python3 /path/to/claude-task-handoff-mcp/run_server.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from task_handoff.server import main  # noqa: E402

if __name__ == "__main__":
    main()
