"""answer_at (#44): a decision or approval says where the person answers: an issue, or a session."""

from __future__ import annotations

import asyncio
import io

import pytest
from mcp import Client

from waypoint import wp
from waypoint.client import WaypointClient
from waypoint.errors import ApiError
from waypoint.mcp_server import build
from waypoint.schema import validator

ISSUE = "https://git.example.org/acme/app/issues/31"
Q = {"text": "Online or offline?"}


def valid(item):
    errors = list(validator("Item").iter_errors(item))
    assert not errors, [e.message for e in errors[:3]]


def decision(mk, caller="agent-b", owner="alice", **kw):
    return mk(owner, "Choose the strategy", caller=caller, kind="decision", question=Q, **kw)


def test_default_is_the_asking_session(store, mk):
    d = decision(mk)
    assert d["answer_at"] == {"session": "agent-b", "default": True}
    valid(d)
    a = mk("alice", "Go for production", caller="coordinator", kind="approval")
    assert a["answer_at"] == {"session": "coordinator", "default": True}


def test_no_default_without_an_agent_creator_or_for_other_kinds(store, mk):
    assert decision(mk, caller="bob")["answer_at"] is None   # a person asked a person
    assert mk("agent-a", "A task")["answer_at"] is None


@pytest.mark.parametrize("where", [{"issue": ISSUE}, {"session": "agent-c"}, {"session": "coordinator"},
                                   {"issue": "https://git.example.org/acme/app/pull/12"},
                                   {"issue": "https://gitlab.example.org/acme/app/-/merge_requests/7"}])
def test_explicit(store, mk, where):
    d = decision(mk, answer_at=where)
    assert d["answer_at"] == {**where, "default": False}
    valid(d)


@pytest.mark.parametrize("where,field,code", [
    ({"session": "alice"}, "answer_at.session", "invalid"),            # a person, not a session
    ({"session": "nobody"}, "answer_at.session", "invalid"),
    ({"issue": "https://git.example.org/acme/app"}, "answer_at", "invalid"),       # not an issue or PR
    ({"issue": "http://git.example.org/acme/app/issues/3"}, "answer_at", "invalid"),  # not https
    ({"issue": "https://git.example.org/acme/app/issues/3?token=abc"}, "answer_at", "invalid"),
    ({"issue": "https://192.0.2.4/acme/app/issues/3"}, "answer_at.issue", "content"),
    ({"session": "agent-a", "issue": ISSUE}, "answer_at", "invalid"),
])
def test_refused(store, mk, where, field, code):
    with pytest.raises(ApiError) as e:
        decision(mk, answer_at=where)
    assert e.value.code == code and e.value.field.startswith(field)


def test_only_for_decisions_and_approvals(store, mk):
    with pytest.raises(ApiError) as e:
        mk("agent-a", "A task", answer_at={"session": "agent-a"})
    assert e.value.field == "answer_at"


def test_an_issue_link_needs_answer_at(store, mk):
    with pytest.raises(ApiError) as e:
        decision(mk, links=[{"url": ISSUE}])
    assert e.value.field == "answer_at"
    assert e.value.message == "answer_at: say where the answer goes: answer_at issue or session"
    assert store.items() == []
    ok = decision(mk, links=[{"url": ISSUE}], answer_at={"issue": ISSUE})
    assert ok["answer_at"]["issue"] == ISSUE
    plain = decision(mk, links=[{"url": "https://docs.example.org/plans/migration"}])  # no issue: default
    assert plain["answer_at"]["default"] is True


def test_patch_sets_changes_and_resets(store, mk, who):
    d = decision(mk)
    out = store.patch_item(who("alice"), d["id"], {"answer_at": {"issue": ISSUE}})
    assert out["answer_at"] == {"issue": ISSUE, "default": False}
    out = store.patch_item(who("alice"), d["id"], {"answer_at": {"session": "agent-c"}})
    assert out["answer_at"] == {"session": "agent-c", "default": False}
    out = store.patch_item(who("alice"), d["id"], {"answer_at": None})
    assert out["answer_at"] == {"session": "agent-b", "default": True}
    # adding an issue link later doesn't refuse the change
    store.patch_item(who("alice"), d["id"], {"links": [{"url": ISSUE}]})


def test_ask_endpoint(server, store, tokens, mk):
    mk("agent-b", "Migration tool", track="infra")
    body = {"human": "alice", "title": "Choose", "question": Q, "links": [{"url": ISSUE}]}
    status, out, _ = server("POST", "/v1/items/W-1/ask", tokens["agent-b"], body)
    assert status == 400 and out["error"]["field"] == "answer_at"
    assert [i["id"] for i in store.items()] == ["W-1"]  # nothing left behind
    status, out, _ = server("POST", "/v1/items/W-1/ask", tokens["agent-b"], {**body, "answer_at": {"issue": ISSUE}})
    assert status == 201 and out["asked"]["answer_at"] == {"issue": ISSUE, "default": False}


def call(server_obj, tool, **args):
    async def run():
        async with Client(server_obj) as c:
            r = await c.call_tool(tool, args)
            return r.is_error, "\n".join(getattr(b, "text", "") for b in r.content)
    return asyncio.run(run())


def test_mcp(server, tokens, mk):
    mk("agent-b", "Migration tool", track="infra")
    b = build(WaypointClient(server.base, tokens["agent-b"], timeout=10), "agent-b")
    err, text = call(b, "wp_ask", human="alice", title="Choose", question="Online?", for_item="W-1", links=[ISSUE])
    assert err and text.endswith("say where the answer goes: answer_at issue or session")
    err, text = call(b, "wp_ask", human="alice", title="Choose", question="Online?", for_item="W-1",
                     links=[ISSUE], answer_at=ISSUE)
    assert not err
    err, text = call(b, "wp_get", id="W-2")
    assert f"answer: comment on {ISSUE}" in text
    alice = build(WaypointClient(server.base, tokens["alice"], timeout=10), "alice")
    call(alice, "wp_update", id="W-2", answer_at="")
    err, text = call(b, "wp_get", id="W-2")
    assert "answer: in session agent-b (default: the asking session)" in text


def test_wp(server, tokens, tracks, mk, tmp_path):
    mk("agent-b", "Migration tool", track="infra")

    def run(who, *argv):
        tf = tmp_path / who
        tf.write_text(tokens[who])
        out, err = io.StringIO(), io.StringIO()
        code = wp.main(["--url", server.base, "--token-file", str(tf), *argv], out, err)
        return code, out.getvalue(), err.getvalue()

    code, _, err = run("agent-b", "ask", "alice", "Choose", "Online?", "--for", "W-1", "--link", ISSUE)
    assert code == 1 and "say where the answer goes" in err
    code, out, _ = run("agent-b", "ask", "alice", "Choose", "Online?", "--for", "W-1", "--link", ISSUE,
                       "--answer-session", "agent-b")
    assert code == 0
    code, out, _ = run("alice", "show", "W-2")
    assert "answer: in session agent-b" in out and "(default" not in out
    code, out, _ = run("alice", "update", "W-2", "--answer-issue", ISSUE)
    code, out, _ = run("alice", "show", "W-2")
    assert f"answer: comment on {ISSUE}" in out
    code, out, _ = run("alice", "update", "W-2", "--answer-default")
    code, out, _ = run("alice", "show", "W-2")
    assert "answer: in session agent-b (default: the asking session)" in out
