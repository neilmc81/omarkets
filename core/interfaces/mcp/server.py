"""A tiny stdlib MCP stdio server exposing Omarkets voice tools (Phase 9).

Speaks newline-delimited JSON-RPC 2.0 over stdin/stdout — the same protocol the
``@modelcontextprotocol/sdk`` stdio transport uses, which is what the Qwen voice
frontend connects to. No third-party dependencies.

Design rules, matching the Omarkets architecture:

* **Read-only.** Every tool reads already-computed Omarkets state over the
  loopback HTTP API. There is no tool for reconfiguring Omarkets, editing
  thresholds, changing model policy, opening databases or running shell.
* **No analysis in the client.** An LLM (Qwen) may present a tool result, but the
  facts and any interpretation come from Omarkets. The server never calls a
  model and never calculates a market value.
* **Fail closed.** An unknown tool is an error; malformed parameters are an
  error; an unreachable API is a short spoken error — never a crash.

The server can also be run directly for local testing::

    OMARKETS_API_URL=http://127.0.0.1:8765 python3 -m core.interfaces.mcp
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
import urllib.parse
from collections.abc import Callable
from typing import Any

from core.interfaces.mcp import tools
from core.interfaces.mcp.client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    MarketOsClient,
    MarketOsProtocolError,
    MarketOsUnavailable,
)
from core.interfaces.mcp.launcher import launch_omarkets

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "omarkets", "version": "0.1.0"}
SERVER_KEY = "omarkets"

#: Tool surface. Deliberately user-level intents, not one tool per endpoint.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "market_status",
        "description": (
            "Current market snapshot for the Omarkets workstation: NQ, ES, "
            "yields, the dollar, VIX and volatility, plus the next major event. "
            "Use for 'what's happening with NQ?', 'how's the market?', "
            "'what's NQ doing?'. Read-only; never triggers analysis."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "market_brief",
        "description": (
            "A prepared concise market briefing assembled from existing "
            "Omarkets state and the latest stored analysis. Use for 'give me the "
            "market brief', 'catch me up', 'what's going on?'. It never starts a "
            "new model analysis."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "market_next_event",
        "description": (
            "The next important economic event (CPI, NFP, FOMC, ...) with its "
            "New York time and, when available, consensus and previous values. "
            "Use for 'what's next?', 'when is CPI?', 'what's the next big "
            "number?'. Never invents consensus."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Optional filter, e.g. 'CPI' or 'payrolls'.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "market_calendar",
        "description": (
            "Economic calendar events, prioritising CRITICAL and HIGH "
            "importance. Use for 'what's on the calendar today?', 'what's "
            "important this week?'. Keep the spoken answer short."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["today", "week", "upcoming"],
                    "description": "Window to summarise. Defaults to today.",
                },
                "include_all": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, include low/medium events too.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "market_macro",
        "description": (
            "Latest macro readings (CPI, employment, growth, policy) from "
            "Omarkets. Use for 'what's the macro picture?', 'what was the "
            "latest CPI?', 'what's happening with employment?'. Optionally filter "
            "to one series id substring (e.g. 'CPI', 'PAYROLL')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "series": {
                    "type": "string",
                    "description": "Optional series id substring, e.g. 'CPI'.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "market_latest_events",
        "description": (
            "The important market news and geopolitical events Omarkets has "
            "ranked as material. Use for 'any important news?', 'what just "
            "happened?', 'anything I should care about?'. Not a raw feed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "How many events to return. Defaults to 5.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "market_latest_analysis",
        "description": (
            "The latest plain-English Omarkets desk note (pre-market, economic "
            "release, Fed, earnings, big move or 'market now'). Use for 'what's "
            "the latest analysis?', 'what does Omarkets think?'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(tools.NOTE_LABELS),
                    "description": "Optional note kind filter, e.g. 'release'.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "market_latest_briefing",
        "description": (
            "The latest stored Omarkets pre-market note, week-ahead note or weekly "
            "review. Use for 'give me the morning brief', 'give me the latest "
            "briefing', 'what's the week ahead?'. This RETRIEVES an existing note; "
            "it never writes a new one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "briefing_type": {
                    "type": "string",
                    "enum": list(tools.BRIEFING_KINDS),
                    "description": "Optional filter; omit for the latest of any type.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "market_history_query",
        "description": (
            "Retired in October 2026; answers that the feature is gone. "
            "A descriptive historical statistic from Omarkets's own stored "
            "outcomes: how NQ behaved after comparable past events. Use for "
            "'how has NQ behaved after hot CPI releases?', 'when yields jump "
            "after CPI, what usually happens to NQ?'. Returns a sample size and "
            "median moves; it is never a trading recommendation and never "
            "predicts the future."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_code": {
                    "type": "string",
                    "description": "Optional event code filter, e.g. 'CPI', 'NFP', 'FOMC'.",
                },
                "materiality": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Optional materiality filter.",
                },
                "surprise": {
                    "type": "string",
                    "enum": ["upside", "downside", "inline"],
                    "description": "Optional release-surprise direction.",
                },
                "rates_direction": {
                    "type": "string",
                    "enum": ["up", "down", "flat"],
                    "description": "Optional US2Y direction filter.",
                },
                "vwap_position": {
                    "type": "string",
                    "enum": ["above_vwap", "below_vwap"],
                    "description": "Optional NQ VWAP-state filter at the anchor.",
                },
                "horizon": {
                    "type": "string",
                    "enum": ["5m", "15m", "30m", "1h", "session_close"],
                    "description": "Outcome horizon. Defaults to 1h.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "market_materiality_calibration",
        "description": (
            "Retired in October 2026; answers that the feature is gone. "
            "How Omarkets's own materiality levels have behaved historically: "
            "sample size and median absolute move per level. Use for 'how have "
            "HIGH materiality events behaved?', 'how reliable has Omarkets been "
            "at identifying material events?'. Descriptive only; no level is "
            "declared right or wrong."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "horizon": {
                    "type": "string",
                    "enum": ["5m", "15m", "30m", "1h", "session_close"],
                    "description": "Outcome horizon. Defaults to 1h.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "market_fomc_history",
        "description": (
            "Retired in October 2026; answers that the feature is gone. "
            "Historical FOMC behaviour: how the initial statement move behaved "
            "afterwards across stored cases. Use for 'how often does the initial "
            "FOMC move reverse?'. Statement and press-conference stages are "
            "measured separately."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "horizon": {
                    "type": "string",
                    "enum": ["5m", "15m", "30m", "1h", "session_close"],
                    "description": "Outcome horizon. Defaults to 5m.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "market_event_analysis",
        "description": (
            "The Omarkets desk note about one event. Pass a note id when known; "
            "otherwise pass a short descriptor of the event just discussed (e.g. "
            "'retail sales') and Omarkets resolves it to a real recent note. Use "
            "for 'explain that CPI release', 'what does Omarkets say about that "
            "event?'. Never invents an event."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "Exact Omarkets desk note id, when known.",
                },
                "descriptor": {
                    "type": "string",
                    "description": "Short description of the recent event to resolve.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "market_usage",
        "description": (
            "Omarkets AI spending this week (desk notes) against its weekly "
            "limit. Use for 'what is Omarkets costing me?'. Separate from voice "
            "billing. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 720,
                    "description": "Trailing window in hours. Defaults to 24.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "market_similar_cases",
        "description": (
            "Retired in October 2026; answers that the feature is gone. "
            "Deterministic historical-similarity search: which past Omarkets "
            "anchors most closely resemble the current market context, with each "
            "match's later NQ outcome. Use for 'find situations similar to the "
            "current market', 'have we seen this kind of CPI setup before?', "
            "'what happened in similar cases?'. The match is computed by code, "
            "never guessed, and the outcomes are context, not a prediction."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "How many similar cases. Defaults to 8.",
                },
                "horizon": {
                    "type": "string",
                    "enum": ["5m", "15m", "30m", "1h", "session_close"],
                    "description": "Outcome horizon. Defaults to 1h.",
                },
                "event_code": {
                    "type": "string",
                    "description": "Optional event-family filter, e.g. 'CPI', 'FOMC'.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "market_calibration_review",
        "description": (
            "Retired in October 2026; answers that the feature is gone. "
            "Whether Omarkets currently recommends reviewing any of its own "
            "thresholds, from deterministic calibration statistics. Use for 'does "
            "Omarkets recommend reviewing anything?', 'show calibration issues', "
            "'is any threshold miscalibrated?'. Recommendations are for a human "
            "and no change is ever applied automatically."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "recommendation_id": {
                    "type": "string",
                    "description": "Optional exact recommendation id for detail.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "open_omarkets",
        "description": (
            "Open or focus Omarkets at a page. Use for 'open Omarkets', 'show "
            "me today's calendar'. Destinations: overview (the Today notes), "
            "macro, calendar, news, analysts (also Today). It focuses an "
            "existing Omarkets window rather than opening a duplicate."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "destination": {
                    "type": "string",
                    "enum": list(tools.OPEN_DESTINATIONS),
                    "description": "Page to open. Defaults to overview.",
                }
            },
            "additionalProperties": False,
        },
    },
]

TOOL_NAMES = tuple(tool["name"] for tool in TOOLS)
#: Pre-rename tool names still accepted on calls for existing voice setups.
TOOL_ALIASES = {"open_marketos": "open_omarkets"}


def listed_tools() -> list[dict[str, Any]]:
    """Tools for tools/list: deprecated aliases are listed too unless
    ``OMARKETS_MCP_LEGACY_TOOLS`` is off (``0``/``false``/``no``/``off``)."""
    flag = os.environ.get("OMARKETS_MCP_LEGACY_TOOLS", "1").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return TOOLS
    by_name = {tool["name"]: tool for tool in TOOLS}
    aliases = [
        {
            **by_name[target],
            "name": alias,
            "description": f"Deprecated alias of {target}. " + by_name[target]["description"],
        }
        for alias, target in TOOL_ALIASES.items()
    ]
    return TOOLS + aliases


class McpServer:
    """JSON-RPC/MCP handler for the Omarkets voice tool surface."""

    def __init__(
        self,
        client: MarketOsClient | None = None,
        *,
        launcher: Callable[[str, str], None] = launch_omarkets,
    ) -> None:
        self.client = client or MarketOsClient(
            os.environ.get("OMARKETS_API_URL")
            or os.environ.get("MARKETOS_API_URL")  # pre-rename name
            or DEFAULT_BASE_URL,
            timeout=_timeout_from_env(),
        )
        self.launcher = launcher

    # -- JSON-RPC ----------------------------------------------------------
    def handle(self, line: str) -> str | None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return self._error(None, "Parse error", -32700)
        if not isinstance(msg, dict):
            return self._error(None, "Parse error", -32700)
        method = msg.get("method")
        msg_id = msg.get("id")
        if method is None and "id" in msg:
            return None

        if method == "initialize" and "id" in msg:
            params = _params_object(msg)
            requested = params.get("protocolVersion") or PROTOCOL_VERSION
            return self._result(
                msg_id,
                {
                    "protocolVersion": requested,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                },
            )
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping" and "id" in msg:
            return self._result(msg_id, {})
        if method == "tools/list" and "id" in msg:
            return self._result(msg_id, {"tools": listed_tools()})
        if method == "tools/call" and "id" in msg:
            return self._call_tool(msg_id, _params_object(msg))
        if method == "resources/list" and "id" in msg:
            return self._result(msg_id, {"resources": []})
        if "id" in msg:
            return self._error(msg_id, f"method not supported: {method}", -32601)
        return None

    def _call_tool(self, msg_id: Any, params: dict[str, Any]) -> str:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return self._tool_error(msg_id, "arguments must be an object")
        if isinstance(name, str):
            name = TOOL_ALIASES.get(name, name)
        if name not in TOOL_NAMES:
            return self._tool_error(msg_id, f"tool not exposed by this server: {name!r}")
        try:
            result = self.dispatch(str(name), args)
        except (MarketOsUnavailable, MarketOsProtocolError) as exc:
            return self._tool_error(msg_id, str(exc))
        except tools.VoiceToolError as exc:
            return self._tool_error(msg_id, str(exc))
        except (ValueError, KeyError, TypeError) as exc:
            return self._tool_error(msg_id, f"invalid parameters: {exc}")
        except Exception as exc:  # noqa: BLE001 - keep the server alive
            traceback.print_exc(file=sys.stderr)
            return self._tool_error(msg_id, f"internal error: {exc}")
        return self._result(
            msg_id,
            {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "isError": False,
            },
        )

    # -- dispatch ----------------------------------------------------------
    def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "market_status":
            return self._market_status()
        if name == "market_brief":
            return self._market_brief()
        if name == "market_next_event":
            return tools.build_next_event(self._upcoming_events(), name=_optional_str(args, "name"))
        if name == "market_calendar":
            scope = _optional_str(args, "scope") or "today"
            if scope not in {"today", "week", "upcoming"}:
                raise ValueError("scope must be today, week or upcoming")
            return tools.build_calendar(
                self._calendar_events(scope),
                scope=scope,
                include_all=_bool_arg(args, "include_all"),
            )
        if name == "market_macro":
            return tools.build_macro(
                self.client.get_json("/api/v1/macro/state"),
                series=_optional_str(args, "series"),
            )
        if name == "market_latest_events":
            return self._latest_events(args)
        if name == "market_latest_analysis":
            return tools.build_latest_analysis(self._notes(), kind=_optional_str(args, "kind"))
        if name == "market_event_analysis":
            return self._event_analysis(args)
        if name == "market_latest_briefing":
            return tools.build_latest_briefing(
                self._notes(), briefing_type=_optional_str(args, "briefing_type")
            )
        if name == "market_usage":
            return tools.build_usage(self.client.get_json("/api/v1/ai/budget"))
        if name in tools.RETIRED_TOOLS:
            return tools.build_retired()
        if name == "open_omarkets":
            view = tools.build_open_omarkets(_optional_str(args, "destination"))
            self.launcher(view["destination"], self.client.base_url)
            return view
        raise tools.VoiceToolError(f"tool not implemented: {name}")

    # -- data access -------------------------------------------------------
    def _market_status(self) -> dict[str, Any]:
        status, context = self._get_many(
            ("/api/v1/shell/status", None),
            ("/api/v1/shell/context", None),
        )
        return tools.build_status(status=status, context=context)

    def _market_brief(self) -> dict[str, Any]:
        status, context, desk = self._get_many(
            ("/api/v1/shell/status", None),
            ("/api/v1/shell/context", None),
            ("/api/v1/desk", {"limit": 5}),
        )
        latest = tools.build_latest_analysis(_notes_from(desk)).get("analysis")
        note = None
        if latest:
            note = next(n for n in _notes_from(desk) if n.get("id") == latest["note_id"])
        return tools.build_brief(status=status, context=context, note=note)

    def _notes(self, limit: int = 30) -> list[dict[str, Any]]:
        return _notes_from(self.client.get_json("/api/v1/desk", {"limit": limit}))

    def _upcoming_events(self) -> list[dict[str, Any]]:
        payload = self.client.get_json("/api/v1/calendar/upcoming", {"days": 30, "limit": 100})
        return _events_from(payload)

    def _calendar_events(self, scope: str) -> list[dict[str, Any]]:
        path = {
            "today": "/api/v1/calendar/today",
            "week": "/api/v1/calendar/week",
            "upcoming": "/api/v1/calendar/upcoming",
        }[scope]
        payload = self.client.get_json(path, {"limit": 200})
        return _events_from(payload)

    def _latest_events(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = _bounded_int(args, "limit", 5, 1, 10)
        latest = self.client.get_json("/api/v1/events/latest", {"limit": limit * 4})
        return tools.build_latest_events(_events_from(latest), now=_utc_now_iso(), limit=limit)

    def _event_analysis(self, args: dict[str, Any]) -> dict[str, Any]:
        note_id = _optional_str(args, "event_id")
        if note_id:
            # The id is a path segment, so encode it: an id containing '/', '?'
            # or '#' must not escape the notes route or select another endpoint.
            encoded = urllib.parse.quote(note_id, safe="")
            return tools.build_note(
                self.client.get_json(f"/api/v1/desk/notes/{encoded}"), note_id=note_id
            )
        descriptor = _optional_str(args, "descriptor")
        if not descriptor:
            raise tools.VoiceToolError(
                "Tell me which event to explain, or ask about a recent event."
            )
        return tools.build_event_note(self._notes(), descriptor)

    def _get_many(self, *requests: tuple[str, dict[str, Any] | None]) -> list[Any]:
        """Fetch independent endpoints concurrently, preserving order.

        Voice turns are latency-sensitive and the client is a blocking stdlib
        call, so serial round trips would sum their timeouts. Threads keep the
        total close to the slowest single request. A failure in one request
        propagates exactly as a serial call would.
        """
        if len(requests) == 1:
            path, params = requests[0]
            return [self.client.get_json(path, params)]

        results: list[Any] = [None] * len(requests)
        errors: list[tuple[int, BaseException]] = []
        lock = threading.Lock()

        def worker(index: int, path: str, params: dict[str, Any] | None) -> None:
            try:
                value = self.client.get_json(path, params)
                # Keep the assignment inside the try: a legitimate payload can
                # be ``None`` (404 / malformed JSON), so a lost exception that
                # left ``results[index]`` untouched would be indistinguishable
                # from a real empty response.
                results[index] = value
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
                with lock:
                    errors.append((index, exc))

        threads = [
            threading.Thread(target=worker, args=(index, path, params), daemon=True)
            for index, (path, params) in enumerate(requests)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            # Deterministic (lowest-index) error, matching serial semantics.
            errors.sort(key=lambda item: item[0])
            raise errors[0][1]
        return results

    # -- encoding helpers --------------------------------------------------
    def _result(self, msg_id: Any, result: dict[str, Any]) -> str:
        return json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}, ensure_ascii=False)

    def _tool_error(self, msg_id: Any, message: str) -> str:
        return self._result(
            msg_id,
            {"content": [{"type": "text", "text": f"ERROR: {message}"}], "isError": True},
        )

    def _error(self, msg_id: Any, message: str, code: int) -> str:
        return json.dumps(
            {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}},
            ensure_ascii=False,
        )


def _notes_from(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("notes") if isinstance(payload, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


def _events_from(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("events"), list):
        return [e for e in payload["events"] if isinstance(e, dict)]
    return []


def _params_object(msg: dict[str, Any]) -> dict[str, Any]:
    """Return ``msg['params']`` only when it is a JSON object.

    A malformed request (``params`` as a list, string or number) must not crash
    the stdio server: the MCP client would lose the whole connection. Treating a
    non-object as empty keeps the failure inside the normal error path.
    """
    params = msg.get("params")
    return params if isinstance(params, dict) else {}


def _optional_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bool_arg(args: dict[str, Any], key: str, default: bool = False) -> bool:
    """Strict JSON-boolean parsing.

    A conforming MCP client sends a real boolean, but a permissive ``bool(...)``
    would treat the string ``"false"`` as true. Accept only a genuine bool.
    """
    value = args.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError(f"{key} must be a boolean")


def _bounded_int(args: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    value = args.get(key)
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return number


def _timeout_from_env() -> float:
    raw = os.environ.get("OMARKETS_MCP_TIMEOUT") or os.environ.get("MARKETOS_MCP_TIMEOUT")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    if value <= 0 or value > 30:
        return DEFAULT_TIMEOUT_SECONDS
    return value


def _utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def serve() -> int:
    server = McpServer()
    for line in sys.stdin:
        if not line or not line.strip():
            continue
        response = server.handle(line.rstrip("\n"))
        if response:
            sys.stdout.write(response + "\n")
            sys.stdout.flush()
    return 0


__all__ = [
    "PROTOCOL_VERSION",
    "SERVER_INFO",
    "SERVER_KEY",
    "TOOLS",
    "TOOL_NAMES",
    "McpServer",
    "serve",
]
