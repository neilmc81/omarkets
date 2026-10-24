"""Omarkets MCP server (Phase 9).

Exposes a small, read-only, user-intent-level voice tool surface over the MCP
stdio protocol. Kept dependency-free (stdlib only) so it can be launched by the
Qwen voice frontend with a plain ``python3``, without the Omarkets virtualenv.

Run it directly::

    python3 -m core.interfaces.mcp
"""

from __future__ import annotations

from core.interfaces.mcp.server import (
    SERVER_INFO,
    SERVER_KEY,
    TOOL_NAMES,
    TOOLS,
    McpServer,
    serve,
)

__all__ = [
    "SERVER_INFO",
    "SERVER_KEY",
    "TOOLS",
    "TOOL_NAMES",
    "McpServer",
    "serve",
]
