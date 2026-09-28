"""Run the task-handoff CLI straight from a git clone (no install), e.g. for Claude Code hooks:

    python3 /path/to/claude-task-handoff-mcp/run_cli.py hook stop
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from task_handoff.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
