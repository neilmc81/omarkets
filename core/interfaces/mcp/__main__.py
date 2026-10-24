"""Entry point: ``python3 -m core.interfaces.mcp`` runs the stdio MCP server."""

from __future__ import annotations

import sys

from core.interfaces.mcp.server import serve

if __name__ == "__main__":
    sys.exit(serve())
