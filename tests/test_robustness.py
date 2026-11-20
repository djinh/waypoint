"""Robustness: the client never follows a redirect and never reads without a limit, token files
and the database are private, local admin writes keep the API's rules, and failures in the store,
the daemon's start and the API are reported clearly instead of hidden or turned into tracebacks."""

from __future__ import annotations

import asyncio
import http.client
import http.server
import logging
import os
import socket
import sqlite3
import stat
import threading

import pytest

from waypoint import client as client_mod
from waypoint import db, server
from waypoint.client import ApiFailure, WaypointClient, read_token
from waypoint.deliveries import Signal
from waypoint.errors import ApiError
from waypoint.events import Broadcaster
from waypoint.monitor import export_json


# ---------------------------------------------------------------- a tiny HTTP server for the client
class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):
        type(self).seen.append((self.server.server_port, self.headers.get("Authorization")))
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.other}/landing")
            self.end_headers()
        elif self.path.startswith("/big"):
            body = b'{"x": "' + b"a" * 200 + b'"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


@pytest.fixture
def two_servers():
    servers = [http.server.HTTPServer(("127.0.0.1", 0), _Handler) for _ in range(2)]
    servers[0].other, servers[1].other = servers[1].server_port, servers[0].server_port
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    _Handler.seen = []
    yield servers
    for s in servers:
        s.shutdown()
        s.server_close()


def test_the_client_never_follows_a_redirect(two_servers):
    a, b = two_servers
    c = WaypointClient(f"http://127.0.0.1:{a.server_port}", "secret-token-value")
    with pytest.raises(ApiFailure) as e:
        c.request("GET", "/redirect")
    assert e.value.status == 302 and "doesn't redirect" in e.value.message
    assert [port for port, _ in _Handler.seen] == [a.server_port]  # the other server saw nothing


def test_the_client_reads_a_bounded_answer(two_servers, monkeypatch):
    a, _ = two_servers
    monkeypatch.setattr(client_mod, "MAX_ANSWER", 100)
    c = WaypointClient(f"http://127.0.0.1:{a.server_port}", "t")
    assert c.request("GET", "/small") == {}
    with pytest.raises(ApiFailure) as e:
        c.request("GET", "/big")
    assert "narrow the request" in e.value.message


def test_a_token_file_others_can_read_works_with_a_warning(tmp_path, capsys):
    p = tmp_path / "t.token"
    p.write_text("tok\n")
    os.chmod(p, 0o600)
    assert read_token(str(p)) == "tok"
    assert capsys.readouterr().err == ""
    os.chmod(p, 0o644)
    assert read_token(str(p)) == "tok"
    captured = capsys.readouterr()
    assert "can be read by other users" in captured.err and captured.out == ""


# ---------------------------------------------------------------- database, store, local admin
def test_a_new_database_is_private(tmp_path):
    old = os.umask(0o022)
    try:
        conn = db.connect(tmp_path / "w.db")
        conn.close()
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "w.db").stat().st_mode) == 0o600


def test_local_client_add_keeps_the_label_rules(store, tokens):
    with pytest.raises(ApiError) as e:
        store.add_client("x1", "agent", label="L" * 41)
    assert e.value.field == "label"
    with pytest.raises(ApiError) as e:
        store.add_client("x2", "agent", label="ghp_" + "a" * 36)
    assert e.value.code == "content"
    with pytest.raises(ApiError) as e:
        store.add_client("x3", "agent", deliverer="agent-a")  # not a deliverer
    assert e.value.field == "deliverer"
    store.add_client("x4", "agent", label="Fine", deliverer="host-1")
    assert store.client_record("x4")["deliverer"] == "host-1"


def test_a_failed_commit_is_not_masked_by_the_rollback(store):
    with pytest.raises(ValueError):
        with store.tx() as c:
            c.execute("COMMIT")  # the transaction ends early, as a failed COMMIT would end it
            raise ValueError("the real error")


def test_a_failing_commit_listener_is_logged(store, mk, caplog):
    def broken(items, tracks):
        raise KeyError("x")

    store.commit_listeners.append(broken)
    with caplog.at_level(logging.ERROR, logger="waypoint.store"):
        mk("agent-a", "x")
    assert "commit listener failed: KeyError" in caplog.text


def test_the_export_is_one_snapshot_and_leaves_no_transaction(store, mk):
    mk("agent-a", "x")
    out = export_json(store)
    assert [it["title"] for it in out["items"]] == ["x"]
    assert not store.conn.in_transaction


def test_a_signal_to_a_closed_loop_is_dropped():
    sig = Signal()
    loop = asyncio.new_event_loop()
    entry = (loop, asyncio.Event())
    sig._waiters.add(entry)
    loop.close()
    sig.fire()  # no RuntimeError, so a write that fires it is never rolled back
    assert entry not in sig._waiters


# ---------------------------------------------------------------- the daemon's start
def test_serve_reports_a_busy_port_without_a_traceback(tmp_path, capsys):
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    port = busy.getsockname()[1]
    try:
        cfgp = tmp_path / "c.toml"
        cfgp.write_text(f'[server]\nport = {port}\n[store]\ndatabase = "{tmp_path / "w.db"}"\n')
        assert server.main(["--config", str(cfgp), "serve"]) == 1
    finally:
        busy.close()
    err = capsys.readouterr().err
    assert f"cannot listen on 127.0.0.1 port {port}" in err and "Traceback" not in err


def test_serve_refuses_a_newer_schema_cleanly(tmp_path, capsys):
    conn = sqlite3.connect(tmp_path / "w.db")
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    cfgp = tmp_path / "c.toml"
    cfgp.write_text(f'[store]\ndatabase = "{tmp_path / "w.db"}"\n')
    assert server.main(["--config", str(cfgp), "serve"]) == 1
    err = capsys.readouterr().err
    assert "newer than this Waypoint" in err and "Traceback" not in err


# ---------------------------------------------------------------- the API
def test_401_names_the_scheme(server):
    st, _, headers = server("GET", "/v1/items")
    assert st == 401 and headers.get("www-authenticate") == "Bearer"


def test_responses_are_not_cached_or_sniffed(server, tokens):
    _, _, headers = server("GET", "/v1/items", tokens["agent-a"])
    assert headers.get("x-content-type-options") == "nosniff" and headers.get("cache-control") == "no-store"


def test_the_event_stream_keeps_its_own_cache_header(server, tokens):
    host, port = server.base.replace("http://", "").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    conn.request("GET", "/v1/events", headers={"Authorization": f"Bearer {tokens['pages']}"})
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("cache-control") == "no-cache"
    assert resp.getheader("x-content-type-options") == "nosniff"
    conn.close()


def test_too_many_event_streams_are_refused(server, tokens, monkeypatch):
    import waypoint.api
    monkeypatch.setattr(waypoint.api, "MAX_STREAMS", 0)
    st, body, _ = server("GET", "/v1/events", tokens["pages"])
    assert st == 503 and body["error"]["code"] == "unavailable"


def test_an_odd_last_event_id_is_a_reset_not_an_error(store):
    b = Broadcaster(store)
    assert b._parse(f"{b.run}-²") == -1 and b._parse(f"{b.run}-x") == -1


@pytest.mark.parametrize("message, status", [("database is locked", 503), ("no such table: items", 500)])
def test_only_a_busy_database_is_try_again(server, tokens, store, mk, monkeypatch, message, status):
    mk("agent-a", "x")

    def fail(*a, **kw):
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr(store, "get_item", fail)
    st, body, _ = server("GET", "/v1/items/W-1", tokens["agent-a"])
    assert st == status and body["error"]["code"] == "unavailable"
