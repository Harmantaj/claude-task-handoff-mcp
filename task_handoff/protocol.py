"""Minimal, dependency-free MCP server over stdio (newline-delimited JSON-RPC 2.0).

Implements the subset this server needs: initialize, ping, tools/list, tools/call,
plus progress heartbeats for long tool calls. Keeping it stdlib-only means the server
runs on any Python >= 3.9 without installing packages, which is what makes it
distributable as a one-click Claude Desktop extension.
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
PROGRESS_INTERVAL_S = 10.0


class ToolError(Exception):
    """An expected tool failure, reported to the model as an isError result."""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., str]
    annotations: dict | None = None

    def listing(self) -> dict:
        entry = {"name": self.name, "description": self.description, "inputSchema": self.input_schema}
        if self.annotations:
            entry["annotations"] = self.annotations
        return entry


class StdioServer:
    def __init__(self, name: str, version: str, instructions: str = "") -> None:
        self.name, self.version, self.instructions = name, version, instructions
        self.tools: dict[str, Tool] = {}
        self._write_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=4)
        self._out = sys.stdout

    def tool(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    # ------------------------------------------------------------------ transport

    def _send(self, message: dict) -> None:
        line = json.dumps(message, ensure_ascii=False)
        with self._write_lock:
            self._out.write(line + "\n")
            self._out.flush()

    def _reply(self, msg_id: Any, result: dict) -> None:
        self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _error(self, msg_id: Any, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})

    def log(self, text: str) -> None:
        print(f"[{self.name}] {text}", file=sys.stderr, flush=True)

    # ------------------------------------------------------------------ dispatch

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[-1]
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
        }
        if self.instructions:
            result["instructions"] = self.instructions
        return result

    def _call_tool(self, msg_id: Any, params: dict) -> None:
        name = params.get("name")
        tool = self.tools.get(name)
        if tool is None:
            self._error(msg_id, -32602, f"Unknown tool: {name}")
            return
        token = (params.get("_meta") or {}).get("progressToken")
        done = threading.Event()

        def heartbeat() -> None:
            # Periodic progress lets clients that reset timeouts on progress wait for long test runs.
            count = 0
            while not done.wait(PROGRESS_INTERVAL_S):
                count += 1
                self._send({"jsonrpc": "2.0", "method": "notifications/progress",
                            "params": {"progressToken": token, "progress": count,
                                       "message": f"{name} still running ({int(count * PROGRESS_INTERVAL_S)}s)"}})

        if token is not None:
            threading.Thread(target=heartbeat, daemon=True).start()
        try:
            text = tool.handler(**(params.get("arguments") or {}))
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        except ToolError as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except TypeError as exc:  # bad/missing arguments
            result = {"content": [{"type": "text", "text": f"Invalid arguments: {exc}"}], "isError": True}
        except Exception as exc:  # report, never crash the server
            self.log(traceback.format_exc())
            result = {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
        finally:
            done.set()
        self._reply(msg_id, result)

    def handle(self, message: dict) -> None:
        method, msg_id, params = message.get("method"), message.get("id"), message.get("params") or {}
        is_request = "id" in message
        if method is None:  # a response to something we never send; ignore
            return
        if not is_request:  # notifications (initialized, cancelled, ...) need no reply
            return
        if method == "initialize":
            self._reply(msg_id, self._initialize(params))
        elif method == "ping":
            self._reply(msg_id, {})
        elif method == "tools/list":
            self._reply(msg_id, {"tools": [t.listing() for t in self.tools.values()]})
        elif method == "tools/call":
            self._pool.submit(self._call_tool, msg_id, params)
        elif method in ("resources/list", "prompts/list"):
            self._reply(msg_id, {method.split("/")[0]: []})
        else:
            self._error(msg_id, -32601, f"Method not found: {method}")

    def serve(self) -> None:
        # Anything printed to stdout by accident would corrupt the protocol stream,
        # so the real stdout is kept for protocol messages and sys.stdout is redirected.
        self._out = sys.stdout
        sys.stdout = sys.stderr
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                self._error(None, -32700, "Parse error")
                continue
            if isinstance(message, list):  # batches (2025-03-26) - handle each
                for item in message:
                    self.handle(item)
            elif isinstance(message, dict):
                self.handle(message)
            else:
                self._error(None, -32600, "Invalid request")
        self._pool.shutdown(wait=True)
