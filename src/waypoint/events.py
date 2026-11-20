"""GET /v1/events: a server-sent stream of changes, so pages update live without polling.

After every committed write the store tells the broadcaster which items and tracks changed; each
becomes one event with a sequence number. Subscribers are asyncio queues on the server's loop. A
client reconnecting with Last-Event-ID gets what it missed from a short buffer, or a `reset` event
when that is gone (it then reloads its views). A reader too slow to keep up gets a `reset` too,
rather than an ever-growing queue.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque

from .store import Store, item_id

BUFFER = 1000
QUEUE = 500


class Broadcaster:
    def __init__(self, store: Store, heartbeat: float = 25.0):
        self.store = store
        self.heartbeat = heartbeat
        self._lock = threading.Lock()
        # Ids are "<run>-<seq>": `run` tells this daemon's run from earlier ones, so a client that
        # reconnects with an id from before a restart gets a reset instead of silently nothing.
        self.run = int(time.time() * 1000)
        self._seq = 0
        self._buffer: deque[tuple[int, str, dict]] = deque(maxlen=BUFFER)
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
        store.commit_listeners.append(self.on_commit)

    @classmethod
    def of(cls, store: Store) -> "Broadcaster":
        if getattr(store, "broadcaster", None) is None:
            store.broadcaster = cls(store)
        return store.broadcaster

    # ------------------------------------------------------------------ publishing
    def on_commit(self, item_nums: set[int], track_ids: set[str]) -> None:
        """Called by the store right after a commit, in the writing thread."""
        events = []
        with self.store.read() as c:
            for num in sorted(item_nums):
                r = c.execute("SELECT num, version, state, updated_at FROM items WHERE num = ?", (num,)).fetchone()
                if r is not None:
                    events.append(("item", {"id": item_id(r["num"]), "version": r["version"], "state": r["state"],
                                            "updated_at": r["updated_at"]}))
        events += [("track", {"id": t}) for t in sorted(track_ids)]
        for kind, data in events:
            self.publish(kind, data)

    def publish(self, kind: str, data: dict) -> None:
        with self._lock:
            self._seq += 1
            ev = (self._seq, kind, data)
            self._buffer.append(ev)
            subs = list(self._subscribers)
        for loop, queue in subs:
            loop.call_soon_threadsafe(self._offer, queue, ev)

    @staticmethod
    def _offer(queue: asyncio.Queue, ev) -> None:
        try:
            queue.put_nowait(ev)
        except asyncio.QueueFull:
            # too slow: drop what is queued and tell the reader to reload
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait((ev[0], "reset", {"reason": "too slow"}))

    # ------------------------------------------------------------------ subscribing
    def _parse(self, last_event_id: str | None) -> int | None:
        """The sequence number in an id this run issued; -1 for anything else (another run, garbage)."""
        if last_event_id is None:
            return None
        run, _, seq = last_event_id.partition("-")
        if run != str(self.run) or not seq.isdigit() or int(seq) > self._seq:
            return -1
        return int(seq)

    def subscribe(self, last_event_id: str | None):
        """Register a queue; returns it with the events to send first (missed ones, or a reset)."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE)
        with self._lock:
            self._subscribers.add((loop, queue))
            backlog = []
            last = self._parse(last_event_id)
            if last == -1:
                backlog = [(self._seq, "reset", {"reason": "unknown event id (Waypoint restarted?)"})]
            elif last is not None and last < self._seq:
                oldest = self._buffer[0][0] if self._buffer else self._seq + 1
                if last + 1 < oldest:
                    backlog = [(self._seq, "reset", {"reason": "missed too much"})]
                else:
                    backlog = [ev for ev in self._buffer if ev[0] > last]
        return (loop, queue), backlog

    def unsubscribe(self, entry) -> None:
        with self._lock:
            self._subscribers.discard(entry)

    def format(self, ev) -> str:
        seq, kind, data = ev
        return f"id: {self.run}-{seq}\nevent: {kind}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"

    async def stream(self, last_event_id: str | None, is_disconnected=None):
        """The event stream for one client. `is_disconnected` (an async callable) is checked every
        second, so a client that went away is let go at once, not at the next heartbeat."""
        entry, backlog = self.subscribe(last_event_id)
        loop = asyncio.get_running_loop()
        try:
            yield ": connected\n\n"
            for ev in backlog:
                yield self.format(ev)
            queue = entry[1]
            next_beat = loop.time() + self.heartbeat
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=max(min(1.0, next_beat - loop.time()), 0))
                except asyncio.TimeoutError:
                    if is_disconnected is not None and await is_disconnected():
                        return
                    if loop.time() >= next_beat:
                        next_beat = loop.time() + self.heartbeat
                        yield ": keepalive\n\n"
                    continue
                yield self.format(ev)
        finally:
            self.unsubscribe(entry)
