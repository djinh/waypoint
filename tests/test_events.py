"""GET /v1/events: changes as server-sent events, resuming with Last-Event-ID, resets, heartbeats."""

from __future__ import annotations

import http.client
import json
import queue
import threading
from urllib.parse import urlsplit

import pytest

from waypoint.events import Broadcaster


class Stream:
    """Reads an event stream in a thread; events() returns what arrived so far as parsed dicts."""

    def __init__(self, base: str, token: str | None, last_id: str | None = None):
        u = urlsplit(base)
        self.conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if last_id:
            headers["Last-Event-ID"] = last_id
        self.conn.request("GET", "/v1/events", headers=headers)
        self.resp = self.conn.getresponse()
        self.q: queue.Queue = queue.Queue()
        self.comments: list[str] = []
        if self.resp.status == 200:
            threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        ev = {}
        try:
            for raw in self.resp:
                line = raw.decode().rstrip("\n")
                if line.startswith(":"):
                    self.comments.append(line)
                elif line == "":
                    if ev:
                        self.q.put(ev)
                    ev = {}
                else:
                    key, _, value = line.partition(": ")
                    ev[key] = json.loads(value) if key == "data" else value
        except Exception:
            pass

    def next(self, timeout=5):
        return self.q.get(timeout=timeout)

    def close(self):
        import socket
        try:
            if self.conn.sock is not None:
                self.conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.conn.close()


@pytest.fixture
def stream(server, tokens):
    opened = []

    def open_(who="pages", last_id=None):
        s = Stream(server.base, tokens.get(who) if who else None, last_id)
        opened.append(s)
        return s

    yield open_
    for s in opened:
        s.close()


def test_changes_arrive_as_events(stream, store, mk, who, tracks):
    s = stream()
    assert s.resp.status == 200 and s.resp.headers["Content-Type"].startswith("text/event-stream")
    a = mk("agent-a", "A")
    ev = s.next()
    assert ev["event"] == "item" and ev["data"]["id"] == a["id"] and ev["data"]["state"] == "next"
    b = mk("agent-b", "B", waits_on=[{"item": a["id"]}])
    assert s.next()["data"]["id"] == b["id"]
    store.patch_item(who("agent-a"), a["id"], {"state": "done"})
    got = {(e["data"]["id"], e["data"]["state"]) for e in (s.next(), s.next())}
    assert got == {(a["id"], "done"), (b["id"], "next")}  # the dependent changed too
    ids = [int(e) for e in (a["id"][2:], b["id"][2:])]
    assert ids == [1, 2]


def test_track_event(stream, store, who, tokens):
    s = stream()
    store.put_track(who("agent-a"), "docs", {"title": "Docs"})
    ev = s.next()
    assert ev["event"] == "track" and ev["data"] == {"id": "docs"}


def test_a_refused_write_sends_nothing(stream, store, mk, who):
    s = stream()
    with pytest.raises(Exception):
        mk("agent-a", "x" * 81)
    with pytest.raises(queue.Empty):
        s.next(timeout=0.5)


def test_resume_with_last_event_id(stream, mk):
    s = stream()
    mk("agent-a", "A")
    first = s.next()
    mk("agent-a", "B")
    mk("agent-a", "C")
    s.close()
    again = stream(last_id=first["id"])
    assert [again.next()["data"]["id"], again.next()["data"]["id"]] == ["W-2", "W-3"]


def test_too_far_back_is_a_reset(stream, store, server):
    b = store.broadcaster
    for n in range(1100):
        b.publish("track", {"id": f"t{n}"})
    s = stream(last_id=f"{b.run}-5")
    ev = s.next()
    assert ev["event"] == "reset" and ev["data"]["reason"] == "missed too much"


def test_a_slow_reader_gets_a_reset():
    import asyncio

    from waypoint.events import QUEUE

    q = asyncio.Queue(maxsize=QUEUE)
    for n in range(QUEUE):
        Broadcaster._offer(q, (n, "item", {"id": f"W-{n}"}))
    Broadcaster._offer(q, (QUEUE, "item", {"id": "W-x"}))
    assert q.qsize() == 1 and q.get_nowait()[1] == "reset"


def test_heartbeat(stream, store):
    store.broadcaster.heartbeat = 0.2
    s = stream()
    import time
    time.sleep(0.7)
    assert ": connected" in s.comments and ": keepalive" in s.comments


@pytest.mark.parametrize("who,status", [(None, 401), ("host-1", 403)])
def test_needs_a_reading_token(stream, who, status):
    assert stream(who).resp.status == status


def test_a_client_that_goes_away_is_let_go(stream, store):
    import time
    s = stream()
    time.sleep(0.2)
    assert len(store.broadcaster._subscribers) == 1
    s.close()
    for _ in range(40):
        if not store.broadcaster._subscribers:
            break
        time.sleep(0.1)
    assert not store.broadcaster._subscribers


@pytest.mark.parametrize("last", ["1-500", "500", "garbage", "{run}-99999"])
def test_an_id_this_run_didnt_issue_is_a_reset(stream, store, mk, last):
    """After a restart a page comes back with an id from the old run: it must reload, not miss."""
    mk("agent-a", "A")
    s = stream(last_id=last.format(run=store.broadcaster.run))
    ev = s.next()
    assert ev["event"] == "reset" and "unknown event id" in ev["data"]["reason"]


def test_ids_carry_the_run(stream, store, mk):
    s = stream()
    mk("agent-a", "A")
    run, _, seq = s.next()["id"].partition("-")
    assert run == str(store.broadcaster.run) and seq.isdigit()
