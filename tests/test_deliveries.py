"""Deliveries: queues per deliverer, leases, acks, withdrawal, routing, silence, and the long-poll."""

from __future__ import annotations

import threading
import time

import pytest

from waypoint.deliveries import Deliveries
from waypoint.errors import ApiError
from waypoint.scheduler import Scheduler
from waypoint.schema import validator
from waypoint.store import Client

HOST1 = Client("host-1", "deliverer")
HOST2 = Client("host-2", "deliverer")


@pytest.fixture
def routed(store, tokens, tracks):
    """agent-a, agent-b, alice and the coordinator are reached through host-1; agent-c has no deliverer."""
    tokens["host-2"] = store.add_client("host-2", "deliverer")
    for name in ("agent-a", "agent-b", "alice", "coordinator"):
        store.route(name, "host-1")
    store.settings.outage_seconds = 10**9
    return tokens


@pytest.fixture
def dl(store, routed) -> Deliveries:
    return Deliveries.of(store)


@pytest.fixture
def sched(store, dl) -> Scheduler:
    s = Scheduler(store)
    s.pass_hooks.append(dl.check_silent)
    return s


def wake(mk, owner="agent-a", title="Check the soak", at="2026-03-10T09:30:00Z", **kw):
    return mk(owner, title, wake_at=at, **kw)


def ids(got):
    return [d["id"] for d in got]


def valid(name, body):
    errors = list(validator(name).iter_errors(body))
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors[:3]]


# ---------------------------------------------------------------------- queues and leases


def test_a_deliverer_gets_only_its_recipients_messages(store, dl, sched, mk):
    wake(mk)
    sched.run_pass()
    assert dl.fetch(HOST2) == []
    (d,) = dl.fetch(HOST1)
    valid("Delivery", d)
    assert (d["id"], d["recipient"], d["kind"], d["item"], d["attempt"]) == ("D-1", "agent-a", "wake", "W-1", 1)
    assert d["summary"] == "Check the soak (agent-a)" and d["late"] is None
    for who in (Client("agent-a", "agent"), Client("pages", "reader")):
        with pytest.raises(ApiError) as e:
            dl.fetch(who)
        assert e.value.code == "forbidden"


def test_a_lease_holds_a_message_back_then_it_comes_again(store, dl, sched, mk):
    wake(mk)
    sched.run_pass()
    (first,) = dl.fetch(HOST1)
    store.clock.advance(seconds=119)
    assert dl.fetch(HOST1) == []
    store.clock.advance(seconds=1)
    (again,) = dl.fetch(HOST1)
    assert again["id"] == first["id"] and again["attempt"] == 2 and again["text"] == first["text"]


def test_oldest_first_and_limit(store, dl, sched, mk):
    wake(mk, title="Second", at="2026-03-10T09:20:00Z")
    wake(mk, title="First", at="2026-03-10T09:10:00Z")
    wake(mk, title="Third", at="2026-03-10T09:25:00Z")
    sched.run_pass()
    got = dl.fetch(HOST1, limit=2)
    assert [d["summary"] for d in got] == ["First (agent-a)", "Second (agent-a)"]
    assert [d["summary"] for d in dl.fetch(HOST1)] == ["Third (agent-a)"]


# ---------------------------------------------------------------------- confirmations


def test_ack_is_final_and_idempotent(store, dl, sched, mk):
    wake(mk)
    sched.run_pass()
    (d,) = dl.fetch(HOST1)
    first = dl.ack(HOST1, d["id"], {"result": "delivered"})
    valid("Ack", first)
    store.clock.advance(minutes=5)
    assert dl.ack(HOST1, d["id"], {"result": "undeliverable"}) == first
    assert dl.fetch(HOST1) == []


@pytest.mark.parametrize("did", ["D-1", "D-999", "nonsense", "W-1"])
def test_ack_of_someone_elses_or_no_message(store, dl, sched, mk, did):
    wake(mk)
    sched.run_pass()
    who = HOST2 if did == "D-1" else HOST1
    with pytest.raises(ApiError) as e:
        dl.ack(who, did, {"result": "delivered"})
    assert e.value.code == "not_found"


def test_ack_detail_is_checked(store, dl, sched, mk):
    wake(mk)
    sched.run_pass()
    with pytest.raises(ApiError) as e:
        dl.ack(HOST1, "D-1", {"result": "undeliverable", "detail": "pane showed 192.0.2.4"})
    assert e.value.code == "content" and e.value.field == "detail"
    with pytest.raises(ApiError) as e:
        dl.ack(HOST1, "D-1", {"result": "maybe"})
    assert e.value.code == "invalid"


def test_undeliverable_is_escalated(store, dl, sched, mk):
    wake(mk)
    sched.run_pass()
    dl.fetch(HOST1)
    dl.ack(HOST1, "D-1", {"result": "undeliverable", "detail": "input box busy for 30 minutes"})
    (esc,) = dl.fetch(HOST1)
    assert (esc["recipient"], esc["kind"], esc["item"]) == ("coordinator", "undeliverable", "W-1")
    assert esc["text"] == "Could not deliver D-1 (wake about W-1) to agent-a: input box busy for 30 minutes."
    # a message to the coordinator that can't be delivered isn't escalated to the coordinator again
    dl.ack(HOST1, esc["id"], {"result": "undeliverable"})
    assert dl.fetch(HOST1) == []


# ---------------------------------------------------------------------- withdrawal


def test_a_client_change_withdraws_reminders(store, dl, sched, mk, who):
    it = wake(mk, due_at="2026-03-10T09:00:00Z")
    sched.run_pass()  # a wake and a nudge
    assert sorted(m["kind"] for m in dl.fetch(HOST1)) == ["nudge", "wake"]
    store.patch_item(who("agent-a"), it["id"], {"note": "on it"})
    store.clock.advance(minutes=5)
    assert dl.fetch(HOST1) == []
    # confirming a withdrawn message is accepted and changes nothing
    assert dl.ack(HOST1, "D-1", {"result": "undeliverable"})["result"] == "undeliverable"
    assert dl.fetch(HOST1) == []


def test_done_withdraws_and_unblocks(store, dl, sched, mk, who):
    it = wake(mk)
    mk("agent-b", "Release", waits_on=[{"item": it["id"]}])
    sched.run_pass()
    store.patch_item(who("agent-a"), it["id"], {"state": "done"})
    got = dl.fetch(HOST1)
    assert [(d["kind"], d["recipient"]) for d in got] == [("unblocked", "agent-b")]


def test_at_risk_warnings_go_when_the_late_item_moves(store, dl, sched, mk, who):
    notes = mk("agent-a", "Notes", due_at="2026-03-10T09:00:00Z", overdue=[{"after": "0m", "action": "flag"}])
    mk("agent-b", "Release", waits_on=[{"item": notes["id"]}])
    sched.run_pass()
    store.patch_item(who("agent-a"), notes["id"], {"due_at": "2026-03-11T09:00:00Z"})
    assert dl.fetch(HOST1) == []


def test_unblocked_is_withdrawn_when_the_item_waits_again(store, dl, mk, who):
    a = mk("agent-a", "A")
    mk("agent-b", "B", waits_on=[{"item": a["id"]}])
    store.patch_item(who("agent-a"), a["id"], {"state": "done"})
    store.patch_item(who("agent-a"), a["id"], {"state": "next"})  # reopened
    assert [d["kind"] for d in dl.fetch(HOST1)] == ["replan"]


# ---------------------------------------------------------------------- routing, lateness, silence


def test_no_deliverer_goes_to_the_coordinator(store, dl, sched, mk):
    wake(mk, owner="agent-c", title="Docs search")
    sched.run_pass()
    (d,) = dl.fetch(HOST1)
    assert d["recipient"] == "coordinator"
    assert d["text"] == "For agent-c (no deliverer registered): Reminder: W-1 (Docs search)"


def test_without_a_reachable_coordinator_it_stays_queued(store, dl, sched, mk):
    store.route("coordinator", None)
    wake(mk, owner="agent-c")
    sched.run_pass()
    assert dl.fetch(HOST1) == []
    store.route("agent-c", "host-2")
    assert [d["recipient"] for d in dl.fetch(HOST2)] == ["agent-c"]


def test_route_checks_the_deliverer(store, routed):
    with pytest.raises(ApiError):
        store.route("agent-a", "agent-b")
    with pytest.raises(ApiError):
        store.route("nobody", "host-1")


def test_recipient_away_is_marked(store, dl, sched, mk):
    dl.fetch(HOST1)  # the deliverer was there at 09:30
    wake(mk, at="2026-03-10T09:40:00Z")
    store.clock.advance(minutes=10)
    sched.run_pass()
    store.clock.advance(minutes=50)  # the host sleeps (and is reported silent meanwhile)
    (d,) = [d for d in dl.fetch(HOST1) if d["kind"] == "wake"]
    assert d["late"] == {"due_at": "2026-03-10T09:40:00Z", "reason": "recipient_away",
                         "down_from": "2026-03-10T09:30:00Z", "down_until": "2026-03-10T10:30:00Z"}
    assert d["text"].endswith("(delivered late, due 10:40)")
    store.clock.advance(minutes=5)
    again = [x for x in dl.fetch(HOST1) if x["kind"] == "wake"]
    assert again[0]["text"] == d["text"]  # marked once, not on every hand-out


def test_silent_deliverer_is_reported_once(store, dl, sched):
    store.clock.advance(minutes=4)
    dl.fetch(HOST1)
    store.clock.advance(minutes=5, seconds=1)
    sched.run_pass()
    store.clock.advance(minutes=1)
    sched.run_pass()
    rows = store.conn.execute("SELECT recipient, kind, text FROM messages").fetchall()
    assert [tuple(r) for r in rows] == [("coordinator", "deliverer_silent",
                                         "Deliverer host-1 has not fetched its messages since 10:34.")]
    # host-2 serves nobody, so its silence doesn't matter; a fetch resets host-1
    dl.fetch(HOST1)
    store.clock.advance(minutes=6)
    sched.run_pass()
    assert store.conn.execute("SELECT COUNT(*) FROM messages WHERE kind = 'deliverer_silent'").fetchone()[0] == 2


# ---------------------------------------------------------------------- over HTTP


def test_deliveries_over_http(server, store, routed, mk, who):
    tokens = routed
    status, out, _ = server("GET", "/v1/deliveries?wait=0", tokens["host-1"])
    assert status == 200 and out == {"deliveries": [], "lease_seconds": 120}
    a = mk("agent-a", "A")
    mk("agent-b", "B", waits_on=[{"item": a["id"]}])
    store.patch_item(who("agent-a"), a["id"], {"state": "done"})
    status, out, _ = server("GET", "/v1/deliveries?wait=0", tokens["host-1"])
    valid("DeliveryBatch", out)
    (d,) = out["deliveries"]
    status, ack, _ = server("POST", f"/v1/deliveries/{d['id']}/ack", tokens["host-1"], {"result": "delivered"})
    assert status == 200 and ack["result"] == "delivered"
    valid("Ack", ack)
    assert server("POST", f"/v1/deliveries/{d['id']}/ack", tokens["host-2"], {"result": "delivered"})[0] == 404
    assert server("GET", "/v1/deliveries?wait=61", tokens["host-1"])[0] == 400
    assert server("GET", "/v1/deliveries?wait=0", tokens["agent-a"])[0] == 403
    assert server("GET", "/v1/deliveries?wait=0&colour=red", tokens["host-1"])[0] == 400


def test_long_poll_answers_when_a_message_falls_due(server, store, routed, mk, who):
    a = mk("agent-a", "A")
    mk("agent-b", "B", waits_on=[{"item": a["id"]}])

    def later():
        time.sleep(0.4)
        store.patch_item(who("agent-a"), a["id"], {"state": "done"})

    threading.Thread(target=later).start()
    t0 = time.monotonic()
    status, out, _ = server("GET", "/v1/deliveries?wait=10", routed["host-1"])
    took = time.monotonic() - t0
    assert status == 200 and [d["kind"] for d in out["deliveries"]] == ["unblocked"]
    assert 0.3 < took < 5


def test_long_poll_times_out_empty(server, routed):
    t0 = time.monotonic()
    status, out, _ = server("GET", "/v1/deliveries?wait=1", routed["host-1"])
    assert status == 200 and out["deliveries"] == []
    assert 0.9 < time.monotonic() - t0 < 5


def test_waiting_long_polls_hold_no_threads(server, routed):
    """Forty deliverer requests waiting don't stop other requests being answered."""
    results = []
    threads = [threading.Thread(target=lambda: results.append(server("GET", "/v1/deliveries?wait=3",
                                                                       routed["host-2"])[0]))
               for _ in range(40)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    assert server("GET", "/v1/items", routed["pages"])[0] == 200
    assert time.monotonic() - t0 < 1.5
    for t in threads:
        t.join(timeout=10)
    assert results == [200] * 40


@pytest.mark.parametrize("by", ["coordinator", "alice"])
def test_others_notes_dont_withdraw_the_owners_reminders(store, dl, sched, mk, who, by):
    it = wake(mk, due_at="2026-03-10T09:00:00Z")
    sched.run_pass()
    store.patch_item(who(by), it["id"], {"note": "chasing"})
    assert sorted(d["kind"] for d in dl.fetch(HOST1)) == ["nudge", "wake"]


def test_done_by_someone_else_withdraws(store, dl, sched, mk, who):
    it = wake(mk)
    sched.run_pass()
    store.patch_item(who("alice"), it["id"], {"state": "done", "note": "did it myself"})
    assert dl.fetch(HOST1) == []


def test_unrouting_sends_waiting_messages_to_the_coordinator(store, dl, sched, mk):
    wake(mk, owner="agent-b", title="Check")
    sched.run_pass()
    dl.fetch(HOST1)  # handed out, under a lease
    store.route("agent-b", None)
    (d,) = dl.fetch(HOST1)  # the lease is dropped with the re-routing
    assert d["recipient"] == "coordinator"
    assert d["text"] == "For agent-b (no deliverer registered): Reminder: W-1 (Check)"


@pytest.mark.parametrize("state", ["done", "dropped"])
@pytest.mark.parametrize("by", ["agent-a", "coordinator", "alice"])
def test_closing_by_anyone_withdraws_and_stops_the_ladder(store, dl, sched, mk, who, state, by):
    it = mk("agent-a", "Notes", due_at="2026-03-10T09:30:00Z")
    sched.run_pass()  # the nudge is queued
    store.patch_item(who(by), it["id"], {"state": state, "note": "closed"})
    store.clock.advance(hours=3)
    sched.run_pass()
    assert [d["kind"] for d in dl.fetch(HOST1) if d["kind"] != "deliverer_silent"] == []
    assert [s["action"] for s in store.get_item(it["id"])["overdue_steps"]] == ["nudge"]


@pytest.mark.parametrize("by", ["coordinator", "alice"])
def test_a_new_due_time_from_someone_else_rearms_and_drops_stale_warnings(store, dl, sched, mk, who, by):
    it = mk("agent-a", "Notes", due_at="2026-03-10T09:30:00Z", overdue=[{"after": "0m", "action": "nudge"},
                                                                          {"after": "0m", "action": "flag"}])
    mk("agent-b", "Waits", waits_on=[{"item": it["id"]}])
    sched.run_pass()  # nudge to agent-a, at_risk to agent-b
    store.patch_item(who(by), it["id"], {"due_at": "2026-03-10T11:00:00Z"})
    assert dl.fetch(HOST1) == []  # "late: due 10:30" would be wrong now
    store.clock.set(store.clock.now().replace(hour=11))
    sched.run_pass()
    got = [d for d in dl.fetch(HOST1) if d["kind"] != "deliverer_silent"]
    assert sorted((d["kind"], d["recipient"]) for d in got) == [("at_risk", "agent-b"), ("nudge", "agent-a")]
    assert all("due 12:00" in d["text"] for d in got)
