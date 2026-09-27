# Task Handoff Verifier (MCP)

**Independently check an AI agent's work before you trust it.** Task Handoff looks at what actually
changed in a local git repository, runs only the checks that matter for those changes, cross-checks
what the agent *claimed*, and gives you a short report you can read in under a minute.

Every statement in the report carries one label:

| Label | Meaning |
|---|---|
| **VERIFIED** | Observed directly: git state, or a check that ran and passed or failed |
| **CLAIMED** | Said by the agent, not independently checked |
| **INFERRED** | Follows from verified facts by a stated rule |
| **NOT TESTED** | Relevant, but nothing exercised it |
| **BLOCKED** | A check was needed but could not run (missing tool, missing deps, timeout) |

A failed, timed-out, blocked or empty ("no tests collected") check is **never** reported as a pass.

Example report (real output from a demo repo, path shortened):

```
## Cap discounts at 50% - FAILED
_~/code/demo-shop · baseline 2026-09-27T22:57:08+0530 @ dc8811b592e9_

**Changes** (1 files, +2/-1) [VERIFIED]
- `shop/pricing.py` modified +2/-1 (backend)

**Checks**
- PASS `python-syntax` - exit code 0 · why: 1 Python file(s) changed (read-only parse)
- FAIL `pytest` - 1 failed, 1 passed in 0.01s (0.1s) · why: tests mapped to changed files (1 test file(s))
    > >       assert apply_discount(100, 10) == 90
    > E       assert 100 == 90
    > tests/test_pricing.py:5: AssertionError
    > FAILED tests/test_pricing.py::test_ten_percent - assert 100 == 90
    > 1 failed, 1 passed in 0.01s

**Evidence**
- [NOT TESTED] No passing test exercised: shop/pricing.py
- [CLAIMED] Capped discounts at 50% in pricing.py; all tests pass
- [INFERRED] 'pricing.py' from the claim was modified (shop/pricing.py).
- [INFERRED] Claim of passing tests is not supported by any test run here.

**Next prompt:** Fix the failing check(s): pytest. Run `~/code/demo-shop/.venv/bin/python -m pytest -q
--no-header -p no:cacheprovider tests/test_pricing.py` to reproduce - key output: ... assert 100 == 90 ...
Add or run tests covering shop/pricing.py. Then call verify_task again.
```

---

## Requirements

- macOS or Linux
- `git` and **Python 3.9+** available as `python3` (macOS: included with the Xcode Command Line Tools)
- Nothing else. The server has **no third-party dependencies**.

> **Why a desktop extension and not a remote "custom connector"?** Custom connectors
> (Settings → Connectors → *Add custom connector*) are servers on the internet that Claude reaches
> by URL. This tool must read your *local* repositories, including uncommitted work, and run your
> local tests. A remote server can't see those, so it ships as a local extension for Claude Desktop
> and a local MCP server for Claude Code.

---

## Install

### Option A: Claude Desktop (one click)

1. Download **`task-handoff-<version>.mcpb`** from the
   [latest release](https://github.com/Harmantaj/claude-task-handoff-mcp/releases/latest).
2. Double-click the file. Claude Desktop opens an install dialog. Alternatively, go to
   **Settings → Extensions**, then **Advanced settings → Install Extension…** and pick the file.
3. Click **Install**.
4. Optional: in the extension's settings, set **Allowed directories** (for example `~/code`).
   The tools then refuse any repository outside those folders. Leave it empty to allow all.
5. Make sure the extension is **enabled**. In a chat, open the tools menu (the slider icon under the
   message box) and check that *Task Handoff Verifier* is toggled on.

### Option B: Claude Code

```bash
git clone https://github.com/Harmantaj/claude-task-handoff-mcp ~/.local/share/claude-task-handoff-mcp
```

```bash
claude mcp add --scope user task-handoff -- python3 ~/.local/share/claude-task-handoff-mcp/run_server.py
```

Check it's connected:

```bash
claude mcp get task-handoff
```

To restrict it to certain folders, append them as arguments, for example
`... run_server.py ~/code ~/work`.

### Option C: any other MCP client

Run `python3 /path/to/run_server.py [ALLOWED_DIR ...]` as a **stdio** MCP server.
Alternatively, `pip install git+https://github.com/Harmantaj/claude-task-handoff-mcp` provides a
`task-handoff-mcp` command.

---

## How to use it

Always give the **absolute path** of the repository.

**1. Before the agent starts work**, ask:

> Use task-handoff `start_task` on `/Users/me/code/shop` with task "Cap discounts at 50%".

This records a baseline (current commit plus a fingerprint of files you already had uncommitted), so
your own in-progress edits aren't blamed on the agent. The baseline is stored inside `.git/` and never
appears as a change.

**2. Let the agent do the work** (in the same chat or anywhere else).

**3. Verify:**

> Use task-handoff `verify_task` on `/Users/me/code/shop` with claims
> ["Capped discounts at 50% in pricing.py", "All tests pass"].

Claude gets the report and should relay the verdict honestly. The **Next prompt** line is a
ready-to-paste instruction for the agent's next round, such as fixing the failing test or adding the
missing coverage.

**Tip for Claude Code:** add this to your project's `CLAUDE.md` so it happens automatically:

```
Before changing code, call task-handoff start_task for this repo. When done, call verify_task
with your claims and report its verdict verbatim. Do not claim success if it is not VERIFIED.
```

### Tools

| Tool | Runs checks? | What it does |
|---|---|---|
| `start_task(repo_path, task)` | no | Record the baseline. Replaces any previous one. |
| `inspect_task_state(repo_path)` | no | Show changed files, categories, risk flags and the checks that *would* run |
| `run_relevant_checks(repo_path, dry_run=false)` | yes | Run just the selected checks |
| `verify_task(repo_path, claims=[], notes="")` | yes | Full verification plus claim cross-checking and the handoff report |
| `summarize_handoff(repo_path)` | no | Re-show the last report without re-running; marks it `[STALE]` if the repo changed since |

All tools accept `format: "json"` for the full structured report.

### Verdicts

`VERIFIED` · `PARTIALLY VERIFIED` (some evidence, gaps remain) · `NOT VERIFIED` · `FAILED` ·
`NO CHANGES` · `NO EXECUTABLE CHANGES` (docs or data only) · `NOT VERIFIED (inspection only)`

### What gets checked

Only checks relevant to the changed files run. The full test suite is never run blindly.

| Changed | Checks |
|---|---|
| Python | read-only syntax parse; pytest on tests matched by name (`foo.py` to `test_foo.py`). Full suite only if dependencies or `conftest.py` changed, or nothing matched (labelled as a fallback). ruff and mypy if configured. |
| JS/TS (nearest `package.json`) | `typecheck` script or `tsc --noEmit`, `lint`, `vitest related` / `jest --findRelatedTests` when possible, else `test`; `build` only when dependencies or config changed |
| Go | `go vet` and `go test` on the changed packages |
| Shell scripts | `bash -n` (parse only), plus `shellcheck` if installed |
| UI files, when the repo already has `playwright.config.*` | `playwright test` on the specs whose names match changed components |

It also flags risky changes: auth code, schema or migrations, dependencies, CI and build config,
deleted files, credential-like files, possible secrets in added lines (reported by file and pattern
name only, never the value), large diffs, and pre-existing uncommitted work that disappeared during
the task.

### Configuration (optional)

Put a `.task-handoff.json` in the repository root:

```json
{
  "verification": { "timeout_seconds": 300, "max_parallel_checks": 3, "full_suite_fallback": true },
  "playwright": { "enabled": true, "run_all_when_untargeted": false },
  "report": { "max_files_listed": 25 }
}
```

---

## Safety

- Uses only read-only git commands. It never commits, pushes, resets, deletes or installs anything.
- **It does run your project's own tests and build scripts.** Those can have side effects
  (network calls, writing files) if your test suite has them. Use **Allowed directories** to limit
  which repositories it can touch.
- Checks run with stdin closed, `CI=1`, a timeout, and a process-group kill on timeout.
- Secrets are redacted from all output that reaches the model.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `pytest is not installed for /usr/bin/python3` (BLOCKED) | The tool uses the repo's `.venv/` or `venv/` interpreter if present, otherwise `python3` on PATH. Create an in-repo venv with your dev dependencies. Poetry, conda and pyenv environments outside the repo are not detected yet. |
| `dependencies not installed (no node_modules …)` | Run `npm install` (or pnpm/yarn) yourself. The tool never installs. |
| `timed out after 120s` | Raise `verification.timeout_seconds` in `.task-handoff.json` |
| `npm is not installed` in Claude Desktop but it works in a terminal | GUI apps get a minimal PATH. The server adds `/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`, `~/.cargo/bin`, `~/go/bin`, `~/.bun/bin` and `~/.volta/bin`; tools installed elsewhere (e.g. via nvm) may not be found. |
| Extension shows an error in Claude Desktop | Check the MCP logs in `~/Library/Logs/Claude/` (`mcp.log` and any `mcp-server-*.log`). A healthy start logs `[task-handoff] v0.1.0 starting; python 3.x`. The most common cause is no `python3` on the system: install the Xcode Command Line Tools (`xcode-select --install`). |
| Everything is attributed to the task | No baseline was recorded. Call `start_task` before the work begins. |

---

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/python -m pytest -q
```

```bash
./scripts/build_mcpb.sh
```

The tests build real temporary git repositories for every scenario in
[docs/TESTING_STRATEGY.md](docs/TESTING_STRATEGY.md) and drive the real server over stdio with the
official MCP client. The last command writes `dist/task-handoff-<version>.mcpb` and needs Node.

Layout: `task_handoff/gitstate.py` (baseline and attribution), `impact.py` (categories and risks),
`checks.py` + `playwright_adapter.py` (check selection), `runner.py` (execution),
`handoff.py` (evidence, verdict, report), `protocol.py` (stdlib MCP stdio server), `server.py` (tools).
The original design spec is in [`docs/`](docs/).

## License

MIT
