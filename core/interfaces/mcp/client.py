"""Minimal loopback HTTP client for the Omarkets API.

Stdlib only, so the MCP server can run wherever ``python3`` runs and never
depends on the Omarkets virtualenv or its dependencies. It only ever talks to
``127.0.0.1``/``localhost`` and only over http, because the Omarkets API binds
loopback-only by construction (``core.config.settings`` rejects any other host).

Failures are translated into :class:`MarketOsUnavailable` with a short, spoken
error string; unknown payloads raise :class:`MarketOsProtocolError`. Nothing here
retries aggressively: a voice client must fail fast rather than hang.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

#: Default loopback API base. Mirrors OMARKETS_API_HOST/PORT defaults.
DEFAULT_BASE_URL = "http://127.0.0.1:8765"

#: Short timeout for simple read tools so a voice turn never waits long.
DEFAULT_TIMEOUT_SECONDS = 4.0

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


class MarketOsUnavailable(RuntimeError):
    """The Omarkets API could not be reached or returned an unusable reply."""


class MarketOsProtocolError(RuntimeError):
    """The API replied, but the payload was not the expected shape."""


@dataclass(frozen=True)
class ApiResponse:
    status: int
    data: Any


def validate_base_url(base_url: str) -> str:
    """Return a normalized loopback base URL or raise ``ValueError``.

    This is the boundary that keeps the MCP server from being pointed at an
    arbitrary remote host: only loopback http(s) is accepted, and the URL must
    carry no credentials, query or fragment.
    """
    candidate = (base_url or "").strip().rstrip("/")
    if not candidate:
        candidate = DEFAULT_BASE_URL
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported Omarkets URL scheme: {parsed.scheme or '(none)'}")
    if parsed.username or parsed.password:
        raise ValueError("Omarkets URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Omarkets URL must not contain a query or fragment")
    host = parsed.hostname or ""
    if host not in _ALLOWED_HOSTS:
        raise ValueError(f"Omarkets URL must be loopback, not {host or '(none)'}")
    return f"{parsed.scheme}://{parsed.netloc}"


class MarketOsClient:
    """Synchronous, read-only client for the Omarkets HTTP API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = validate_base_url(base_url)
        self.timeout = float(timeout)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a JSON object from the API, raising a short, spoken-safe error."""
        if not path.startswith("/"):
            raise ValueError(f"path must start with '/': {path!r}")
        url = self.base_url + path
        if params:
            clean = {key: value for key, value in params.items() if value is not None}
            if clean:
                url = url + "?" + urllib.parse.urlencode(clean)
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            # A structured 4xx is still useful: propagate the status so the tool
            # can return "not found" rather than "unreachable".
            return ApiResponse(status=exc.code, data=_safe_json(exc.read()))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MarketOsUnavailable("Omarkets isn't responding right now.") from exc
        if status >= 500:
            raise MarketOsUnavailable("Omarkets isn't responding right now.")
        return ApiResponse(status=status, data=_safe_json(body))

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET and require a 2xx JSON object; 404 becomes ``None``.

        A 5xx is a Omarkets-side fault and is reported as ``unavailable`` so a
        voice client says "Omarkets isn't responding" rather than "bad request".
        Any other 4xx is a contract error.
        """
        response = self.get(path, params)
        if isinstance(response, ApiResponse):
            if response.status == 404:
                return None
            if response.status >= 500:
                raise MarketOsUnavailable("Omarkets isn't responding right now.")
            if response.status >= 400:
                raise MarketOsProtocolError(f"Omarkets returned {response.status} for {path}")
            return response.data
        return response

    def post_json(self, path: str, body: dict[str, Any]) -> Any:
        """POST a JSON body and require a 2xx JSON object.

        Used only for read-only cohort queries (the historical query endpoint
        takes a typed filter object). No mutating endpoint is reachable.
        """
        if not path.startswith("/"):
            raise ValueError(f"path must start with '/': {path!r}")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                raise MarketOsUnavailable("Omarkets isn't responding right now.") from exc
            raise MarketOsProtocolError(f"Omarkets returned {exc.code} for {path}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MarketOsUnavailable("Omarkets isn't responding right now.") from exc
        if status >= 500:
            raise MarketOsUnavailable("Omarkets isn't responding right now.")
        return _safe_json(payload)


def _safe_json(raw: Any) -> Any:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


__all__ = [
    "ApiResponse",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "MarketOsClient",
    "MarketOsProtocolError",
    "MarketOsUnavailable",
    "validate_base_url",
]
