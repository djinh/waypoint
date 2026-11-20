"""A small client for Waypoint's HTTP API, for the deliverer, the MCP server and `wp`.

Standard library only. The token is read from a file and only ever sent in the Authorization
header; it never appears in an error message or a log line.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request


class Unavailable(Exception):
    """Waypoint could not be reached (down, refused, timed out). The message is always the same."""

    def __init__(self):
        super().__init__("Waypoint unavailable")


class ApiFailure(Exception):
    """Waypoint answered with an error in the contract's shape."""

    def __init__(self, status: int, code: str, message: str, field: str | None = None,
                 cycle: list[str] | None = None):
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.field = field
        self.cycle = cycle


def read_token(path: str) -> str:
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        token = f.read().strip()
    if not token:
        raise ValueError("the token file is empty")
    return token


class WaypointClient:
    def __init__(self, url: str, token: str, timeout: float = 30):
        self.base = url.rstrip("/")
        self._token = token
        self.timeout = timeout

    def __repr__(self) -> str:  # never show the token
        return f"WaypointClient({self.base!r})"

    @classmethod
    def from_token_file(cls, url: str, token_file: str, timeout: float = 30) -> "WaypointClient":
        return cls(url, read_token(token_file), timeout)

    def request(self, method: str, path: str, body=None, query: dict | None = None, timeout: float | None = None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            raise self._failure(e) from None
        except (urllib.error.URLError, OSError, socket.timeout):
            raise Unavailable() from None
        try:
            return json.loads(payload) if payload else None
        except ValueError:
            raise Unavailable() from None

    @staticmethod
    def _failure(e: urllib.error.HTTPError) -> Exception:
        try:
            err = json.loads(e.read())["error"]
            return ApiFailure(e.code, err["code"], err["message"], err.get("field"), err.get("cycle"))
        except Exception:
            if e.code >= 500:
                return Unavailable()
            return ApiFailure(e.code, "invalid", f"HTTP {e.code}")
