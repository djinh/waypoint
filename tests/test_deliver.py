"""waypoint-deliver: the loop, de-duplication, giving up after 30 minutes, back-off, config."""

from __future__ import annotations

import re

import pytest

from waypoint import deliver
from waypoint.adapters import DELIVERED, RETRY, Undeliverable
from waypoint.client import ApiFailure, Unavailable, WaypointClient
from waypoint.deliver import Deliverer


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeClient:
    """Answers fetches from a script and records confirmations."""

    def __init__(self):
        self.batches = []
        self.acks = []
        self.ack_fails = 0

    def request(self, method, path, body=None, query=None, timeout=None):
        if method == "GET":
            if not self.batches:
                return {"deliveries": [], "lease_seconds": 120}
            nxt = self.batches.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return {"deliveries": nxt, "lease_seconds": 120}
        if self.ack_fails:
            self.ack_fails -= 1
            raise Unavailable()
        self.acks.append((path.split("/")[3], body))
        return {}


class Adapter:
    def __init__(self, *results):
        self.results = list(results)
        self.seen = []

    def deliver(self, d):
        self.seen.append(d["id"])
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, Exception):
            raise r
        return r


def msg(did="D-1", recipient="agent-a", attempt=1, **kw):
    return {"id": did, "recipient": recipient, "kind": "wake", "attempt": attempt, "text": "Reminder", "summary": "S",
            **kw}


def setup(*results, recipient="agent-a"):
    client, clock, adapter = FakeClient(), Clock(), Adapter(*results)
    return client, clock, adapter, Deliverer(client, {recipient: adapter}, clock=clock)


def round_(d, wait=0):
    d.poll(wait)
    d.work()


def test_delivered_and_confirmed():
    client, clock, adapter, d = setup(DELIVERED)
    client.batches = [[msg()]]
    round_(d)
    assert adapter.seen == ["D-1"] and client.acks == [("D-1", {"result": "delivered"})]
    assert d.pending == {} and d.unconfirmed == {}


def test_a_repeat_while_working_on_it_is_ignored():
    client, clock, adapter, d = setup(RETRY)
    client.batches = [[msg()], [msg(attempt=2)]]
    round_(d)
    round_(d)  # handed out again, but our retry time hasn't come
    assert adapter.seen == ["D-1"]
    clock.t += 5
    round_(d)
    assert adapter.seen == ["D-1", "D-1"]


def test_a_lost_confirmation_is_not_a_second_delivery():
    client, clock, adapter, d = setup(DELIVERED)
    client.batches = [[msg()], [msg(attempt=2)]]
    client.ack_fails = 1
    round_(d)
    assert client.acks == [] and "D-1" in d.unconfirmed
    round_(d)  # the message comes again after its lease: confirm, don't deliver again
    assert adapter.seen == ["D-1"]
    assert client.acks == [("D-1", {"result": "delivered"})]


def test_gives_up_after_30_minutes_from_the_first_hand_out():
    client, clock, adapter, d = setup(RETRY)
    client.batches = [[msg()]]
    round_(d)
    clock.t += 29 * 60 + 59
    round_(d)
    assert client.acks == []
    clock.t += 1
    round_(d)
    assert client.acks == [("D-1", {"result": "undeliverable", "detail": "no chance to deliver within 30 minutes"})]


def test_a_message_handed_out_before_counts_from_then():
    """After a restart the deliverer doesn't know when it first saw a message; `attempt` tells it
    roughly how many leases ago that was."""
    client, clock, adapter, d = setup(RETRY)
    client.batches = [[msg(attempt=15)]]  # 14 leases of 120 s = 28 minutes ago
    round_(d)
    clock.t += 2 * 60
    round_(d)
    assert client.acks[0][1]["result"] == "undeliverable"


def test_unknown_recipient_is_undeliverable_at_once():
    client, clock, adapter, d = setup(DELIVERED)
    client.batches = [[msg(recipient="agent-z")]]
    round_(d)
    assert client.acks == [("D-1", {"result": "undeliverable", "detail": "no adapter configured for this recipient"})]


def test_an_adapter_error_is_retried_and_logged_by_type(caplog):
    client, clock, adapter, d = setup(ValueError("pane text: secret"), DELIVERED)
    client.batches = [[msg()]]
    round_(d)
    assert client.acks == []
    assert "adapter failed on D-1: ValueError" in caplog.text and "secret" not in caplog.text
    clock.t += 5
    round_(d)
    assert client.acks == [("D-1", {"result": "delivered"})]


def test_adapter_undeliverable_detail_goes_with_the_ack():
    client, clock, adapter, d = setup(Undeliverable("the endpoint refused it (HTTP 404)"))
    client.batches = [[msg()]]
    round_(d)
    assert client.acks == [("D-1", {"result": "undeliverable", "detail": "the endpoint refused it (HTTP 404)"})]


def test_backs_off_while_waypoint_is_down():
    client, clock, adapter, d = setup(DELIVERED)
    client.batches = [Unavailable(), Unavailable(), Unavailable(), [msg()]]
    sleeps = []
    rounds = iter(range(10))
    d.run(stop=lambda: next(rounds) >= 4, sleep=sleeps.append)
    assert sleeps == [1, 2, 4]
    assert client.acks == [("D-1", {"result": "delivered"})]


def test_short_polls_while_work_is_pending():
    waits = []

    class Recording(FakeClient):
        def request(self, method, path, body=None, query=None, timeout=None):
            if method == "GET":
                waits.append(query["wait"])
            return super().request(method, path, body, query, timeout)

    client, clock, adapter = Recording(), Clock(), Adapter(RETRY)
    d = Deliverer(client, {"agent-a": adapter}, clock=clock)
    client.batches = [[msg()]]
    rounds = iter(range(10))
    d.run(stop=lambda: next(rounds) >= 2, sleep=lambda s: None)
    assert waits == [60, 5]


# ---------------------------------------------------------------------- against a real server


def test_end_to_end(server, store, tokens, tracks, mk, who):
    store.route("agent-b", "host-1")
    base = server.base
    d = Deliverer(WaypointClient(base, tokens["host-1"]), {"agent-b": Adapter(DELIVERED)})
    a = mk("agent-a", "A")
    mk("agent-b", "B", waits_on=[{"item": a["id"]}])
    store.patch_item(who("agent-a"), a["id"], {"state": "done"})
    round_(d)
    row = store.conn.execute("SELECT recipient, kind, ack_result FROM messages").fetchone()
    assert tuple(row) == ("agent-b", "unblocked", "delivered")


def test_client_errors(server, tokens):
    c = WaypointClient(server.base, "not-a-token")
    with pytest.raises(ApiFailure) as e:
        c.request("GET", "/v1/items")
    assert e.value.code == "unauthorized" and "not-a-token" not in str(e.value)
    assert "not-a-token" not in repr(c)
    with pytest.raises(Unavailable) as e:
        WaypointClient("http://127.0.0.1:9", tokens["alice"], timeout=2).request("GET", "/v1/items")
    assert str(e.value) == "Waypoint unavailable"


# ---------------------------------------------------------------------- configuration


def test_example_config_loads(tmp_path, monkeypatch):
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "deliver.example.toml"), encoding="utf-8").read()
    tok = tmp_path / "token"
    tok.write_text("t0ken\n")
    cfg = tmp_path / "deliver.toml"
    cfg.write_text(text.replace('"/etc/waypoint/host-1.token"', f'"{tok}"'))
    client, adapters = deliver.load(str(cfg))
    assert sorted(adapters) == ["agent-a", "alice", "ops-bot"]
    assert client.base == "http://127.0.0.1:7130"


@pytest.mark.parametrize("body,err", [
    ('[recipient.a]\nadapter = "carrier-pigeon"\n', "adapter must be"),
    ('[recipient.a]\nadapter = "tmux"\n', "missing target"),
    ('[recipient.a]\nadapter = "push"\nurl = "file:///etc/passwd"\n', "url must be http(s)"),
])
def test_bad_configs(tmp_path, body, err):
    tok = tmp_path / "token"
    tok.write_text("t0ken\n")
    cfg = tmp_path / "d.toml"
    cfg.write_text(f'[waypoint]\ntoken_file = "{tok}"\n{body}')
    with pytest.raises(deliver.ConfigError, match=re.escape(err)):
        deliver.load(str(cfg))


def test_main_reports_config_errors(tmp_path, capsys):
    cfg = tmp_path / "d.toml"
    cfg.write_text("[waypoint]\n")
    assert deliver.main(["--config", str(cfg)]) == 2
    assert "token_file is required" in capsys.readouterr().err


def test_a_busy_recipient_is_waited_for_hours_not_minutes():
    from waypoint.adapters import BUSY
    client, clock, adapter, d = setup(BUSY)
    client.batches = [[msg()]]
    round_(d)
    clock.t += 3 * 3600
    round_(d)
    assert client.acks == []  # still waiting after 3 hours: the session is working
    adapter.results = [DELIVERED]
    clock.t += 5
    round_(d)
    assert client.acks == [("D-1", {"result": "delivered"})]


def test_busy_for_four_hours_is_reported():
    from waypoint.adapters import BUSY
    client, clock, adapter, d = setup(BUSY)
    client.batches = [[msg()]]
    round_(d)
    clock.t += 4 * 3600
    round_(d)
    assert client.acks == [("D-1", {"result": "undeliverable", "detail": "the recipient stayed busy for 4 hours"})]


def test_blocked_time_counts_from_when_blocking_began():
    """After a long turn, text left in the box (or someone starting to type) gets its own 30 minutes."""
    from waypoint.adapters import BUSY
    client, clock, adapter, d = setup(BUSY)
    client.batches = [[msg()]]
    round_(d)
    clock.t += 3600                     # an hour-long turn
    adapter.results = [RETRY]           # idle now, but something is in the box
    round_(d)
    assert client.acks == []
    clock.t += 29 * 60
    round_(d)
    assert client.acks == []
    clock.t += 60
    round_(d)
    assert client.acks == [("D-1", {"result": "undeliverable", "detail": "no chance to deliver within 30 minutes"})]


def test_busy_again_resets_the_blocked_clock_but_not_the_4_hours():
    from waypoint.adapters import BUSY
    client, clock, adapter, d = setup(BUSY)
    client.batches = [[msg()]]
    round_(d)
    for _ in range(14):                 # 15-minute stretches, busy and blocked in turn
        clock.t += 15 * 60
        adapter.results = [RETRY if adapter.results[0] == BUSY else BUSY]
        round_(d)
    assert client.acks == []            # 3.5 hours, never blocked for 30 minutes at a stretch
    clock.t += 30 * 60
    round_(d)
    assert client.acks == [("D-1", {"result": "undeliverable", "detail": "the recipient stayed busy for 4 hours"})]


def test_never_busy_still_counts_from_the_first_hand_out():
    client, clock, adapter, d = setup(RETRY)
    client.batches = [[msg(attempt=15)]]  # first handed out about 28 minutes ago
    round_(d)
    clock.t += 2 * 60
    round_(d)
    assert client.acks[0][1]["detail"] == "no chance to deliver within 30 minutes"



def test_after_a_clear_the_next_try_waits_a_minute():
    from waypoint.adapters import CLEARED
    client, clock, adapter, d = setup(CLEARED)
    client.batches = [[msg()]]
    round_(d)
    assert adapter.seen == ["D-1"]
    clock.t += 59
    round_(d)
    assert adapter.seen == ["D-1"]  # not retyped every few seconds
    clock.t += 1
    round_(d)
    assert adapter.seen == ["D-1", "D-1"]


def test_clears_count_as_blocked_for_the_30_minutes():
    from waypoint.adapters import CLEARED
    client, clock, adapter, d = setup(CLEARED)
    client.batches = [[msg()]]
    for _ in range(31):
        round_(d)
        clock.t += 60
    assert client.acks == [("D-1", {"result": "undeliverable", "detail": "no chance to deliver within 30 minutes"})]


def test_settling_tells_the_adapter():
    from waypoint.adapters import CLEARED
    client, clock, adapter, d = setup(CLEARED, DELIVERED)
    settled = []
    adapter.settled = settled.append
    client.batches = [[msg()]]
    round_(d)
    clock.t += 60
    round_(d)
    assert settled == ["D-1"] and client.acks == [("D-1", {"result": "delivered"})]
