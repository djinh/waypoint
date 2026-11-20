"""The agent that asked a person a question may close it, quoting the answer."""

from __future__ import annotations

import asyncio
import io

import pytest
from mcp import Client

from waypoint import wp
from waypoint.client import WaypointClient
from waypoint.errors import ApiError
from waypoint.mcp_server import build

GOOD = 'answered in session agent-b: "online, tonight after nine"'


@pytest.fixture
def question(mk):
    """agent-b asks alice; agent-b's own item waits on the answer."""
    tool = mk("agent-b", "Migration tool", track="infra")
    q = mk("alice", "Choose the migration strategy", caller="agent-b", kind="decision", question={"text": "Which?"})
    return q, tool


def refused(fn, *args, code="forbidden"):
    with pytest.raises(ApiError) as e:
        fn(*args)
    assert e.value.code == code
    return e.value


# the four cases of the issue

def test_the_asker_closes_with_a_quote(store, who, question):
    q, _ = question
    out = store.patch_item(who("agent-b"), q["id"], {"state": "done", "note": GOOD})
    assert out["state"] == "done" and out["note"] == GOOD and out["updated_by"] == "agent-b"
    last = store.get_item(q["id"], history=True)["history"][-1]
    assert last["by"] == "agent-b" and last["event"] == "changed" and last["changes"]["state"] == ["next", "done"]


@pytest.mark.parametrize("note", [
    "alice said online, in the session",            # no quote
    '"online"',                                     # no place
    'she said "" in the session',                   # an empty quote
    None,
])
def test_the_asker_without_a_quote_and_a_place_is_refused(store, who, question, note):
    q, _ = question
    body = {"state": "done"} | ({"note": note} if note is not None else {})
    e = refused(store.patch_item, who("agent-b"), q["id"], body, code="invalid")
    assert e.field == "note" and "quote the answer" in e.message
    assert store.get_item(q["id"])["state"] == "next"


def test_another_agent_is_refused(store, who, question):
    q, _ = question
    refused(store.patch_item, who("agent-a"), q["id"], {"state": "done", "note": GOOD})


def test_a_coordinator_closes_as_before(store, who, question):
    q, _ = question
    out = store.patch_item(who("coordinator"), q["id"], {"state": "done", "note": "closed for agent-b"})
    assert out["state"] == "done"


# and nothing else changes

@pytest.mark.parametrize("body", [
    {"note": GOOD},                                           # not closing
    {"state": "done", "note": GOOD, "title": "Changed"},      # more than closing
    {"state": "dropped", "note": GOOD},                       # dropping isn't answering
    {"state": "running"},
])
def test_the_asker_may_do_nothing_else(store, who, question, body):
    q, _ = question
    refused(store.patch_item, who("agent-b"), q["id"], body)


@pytest.mark.parametrize("closed", [{"state": "dropped", "note": "not needed any more"},
                                    {"state": "done", "note": "offline window, my call"}])
def test_not_after_the_person_closed_or_dropped_it(store, who, question, closed):
    """The person's own decision stands: the asker can't turn a drop into done, or replace their note."""
    q, tool = question
    store.patch_item(who("agent-b"), tool["id"], {"add_waits_on": [{"item": q["id"]}]})
    store.patch_item(who("alice"), q["id"], closed)
    refused(store.patch_item, who("agent-b"), q["id"], {"state": "done", "note": GOOD})
    after = store.get_item(q["id"])
    assert (after["state"], after["note"]) == (closed["state"], closed["note"])
    assert store.get_item(tool["id"])["state"] == ("next" if closed["state"] == "done" else "waiting")


def test_only_questions_owned_by_a_person(store, mk, who):
    task = mk("alice", "Renew the domain", caller="agent-b")                    # a person's item, no question
    refused(store.patch_item, who("agent-b"), task["id"], {"state": "done", "note": GOOD})
    agents = mk("agent-c", "Pick a library", caller="agent-b", kind="decision", question={"text": "Which?"})
    refused(store.patch_item, who("agent-b"), agents["id"], {"state": "done", "note": GOOD})  # owner isn't a person


@pytest.mark.parametrize("note", [
    "answered on https://git.example.org/acme/app/issues/31: “offline window”",
    'answer in acme/app#31: "go ahead"',
    'Alice in the issue: "yes, version 2"',
])
def test_other_forms_of_quote_and_place(store, who, question, note):
    q, _ = question
    assert store.patch_item(who("agent-b"), q["id"], {"state": "done", "note": note})["state"] == "done"


def test_closing_unblocks_the_askers_item(store, who, question):
    q, tool = question
    store.patch_item(who("agent-b"), tool["id"], {"add_waits_on": [{"item": q["id"]}]})
    assert store.get_item(tool["id"])["state"] == "waiting"
    store.patch_item(who("agent-b"), q["id"], {"state": "done", "note": GOOD})
    assert store.get_item(tool["id"])["state"] == "next"


def test_over_http_mcp_and_wp(server, store, tokens, mk, tmp_path):
    mk("agent-b", "Migration tool", track="infra")
    q = mk("alice", "Choose", caller="agent-b", kind="decision", question={"text": "Which?"})
    status, out, _ = server("PATCH", f"/v1/items/{q['id']}", tokens["agent-a"], {"state": "done", "note": GOOD})
    assert status == 403
    status, out, _ = server("PATCH", f"/v1/items/{q['id']}", tokens["agent-b"], {"state": "done", "note": "done"})
    assert status == 400 and out["error"]["field"] == "note"
    b = build(WaypointClient(server.base, tokens["agent-b"], timeout=10), "agent-b")

    async def done(iid, note):
        async with Client(b) as c:
            r = await c.call_tool("wp_done", {"id": iid, "note": note})
            return r.is_error

    assert asyncio.run(done(q["id"], GOOD)) is False
    q2 = mk("alice", "Choose again", caller="agent-b", kind="decision", question={"text": "Which?"})
    tf = tmp_path / "t"
    tf.write_text(tokens["agent-b"])
    assert wp.main(["--url", server.base, "--token-file", str(tf), "done", q2["id"], GOOD],
                   io.StringIO(), io.StringIO()) == 0
    assert store.get_item(q2["id"])["state"] == "done"
