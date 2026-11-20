"""Shared fixtures: a fake clock, a store in tmp_path, clients with tokens, a server on loopback."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

from waypoint.clock import FakeClock
from waypoint.db import connect
from waypoint.store import Client, Settings, Store

T0 = datetime(2026, 3, 10, 9, 30, tzinfo=timezone.utc)

CLIENTS = {
    "agent-a": "agent",
    "agent-b": "agent",
    "agent-c": "agent",
    "alice": "human",
    "bob": "human",
    "coordinator": "coordinator",
    "pages": "reader",
    "host-1": "deliverer",
}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
def store(tmp_path, clock) -> Store:
    st = Store(connect(tmp_path / "waypoint.db"), clock, Settings(tz="Europe/Paris"))
    yield st
    st.conn.close()


@pytest.fixture
def tokens(store) -> dict[str, str]:
    return {name: store.add_client(name, role, label=name.title()) for name, role in CLIENTS.items()}


@pytest.fixture
def who(store, tokens):
    """who('agent-a') -> the Client, for calling the store directly."""
    return lambda name: Client(name, CLIENTS[name], name.title())


@pytest.fixture
def tracks(store, who):
    store.put_track(who("agent-a"), "release", {"title": "Release 2.4", "lead": "agent-a", "order": 10})
    store.put_track(who("agent-b"), "infra", {"title": "Build infrastructure", "lead": "agent-b", "order": 20})
    return ["release", "infra"]


@pytest.fixture
def mk(store, who, tracks):
    """mk('agent-a', 'title', **fields) creates an item and returns it."""

    def make(owner: str, title: str, caller: str | None = None, **fields):
        body = {"track": fields.pop("track", "release"), "title": title, "kind": fields.pop("kind", "task"),
                "owner": owner, **fields}
        return store.create_item(who(caller or owner), body)

    return make


class Http:
    """A tiny JSON client for the test server (urllib only)."""

    def __init__(self, base: str):
        self.base = base

    def __call__(self, method: str, path: str, token: str | None = None, body=None, raw: bytes | None = None,
                 headers: dict | None = None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null"), dict(resp.headers)
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                parsed = json.loads(payload)
            except ValueError:
                parsed = payload.decode(errors="replace")
            return e.code, parsed, dict(e.headers)


@pytest.fixture
def server(store):
    """The real app under uvicorn on 127.0.0.1, a port the OS hands out."""
    import uvicorn

    from waypoint.api import create_app

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(store), log_level="warning", lifespan="off",
                                        timeout_graceful_shutdown=2))
    thread = threading.Thread(target=srv.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.01)
    yield Http(f"http://127.0.0.1:{port}")
    srv.should_exit = True
    thread.join(timeout=10)
    sock.close()


@pytest.fixture(autouse=True)
def no_settle_wait(monkeypatch):
    """The tmux adapter waits a moment after Enter to check the line was submitted; fakes don't need it.
    Tests with a real tmux set their own settle time."""
    from waypoint.adapters import TmuxAdapter
    monkeypatch.setattr(TmuxAdapter, "DEFAULT_SETTLE", 0.0)
