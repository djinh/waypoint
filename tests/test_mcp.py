"""The MCP server: nine short tools, thin over the API, through the SDK's in-process client."""

from __future__ import annotations

import asyncio

import pytest
from mcp import Client

from waypoint.client import WaypointClient
from waypoint.mcp_server import build, to_condition, to_ts

TOOLS = {"wp_mine", "wp_list", "wp_get", "wp_add", "wp_update", "wp_done", "wp_ask", "wp_messages", "wp_recur"}


def call(server, tool, **args):
    """(is_error, text) of one tool call."""

    async def run():
        async with Client(server) as c:
            r = await c.call_tool(tool, args)
            return r.is_error, "\n".join(getattr(b, "text", "") for b in r.content)

    return asyncio.run(run())


@pytest.fixture
def mcp_for(server, tokens, tracks):
    return lambda name: build(WaypointClient(server.base, tokens[name], timeout=10), name)


def test_nine_short_tools(mcp_for):
    async def run():
        async with Client(mcp_for("agent-a")) as c:
            return (await c.list_tools()).tools

    tools = asyncio.run(run())
    assert {t.name for t in tools} == TOOLS
    for t in tools:
        assert len(t.description) <= 200, t.name  # every word costs context in every session


def test_add_mine_done(mcp_for, store):
    a = mcp_for("agent-a")
    err, text = call(a, "wp_add", title="Release notes for 2.4", track="release", label="release notes",
                     due="2026-03-10T11:00:00Z", note="draft half done")
    assert not err and text.startswith("Added W-1 [next] Release notes for 2.4 (agent-a, release)")
    b = mcp_for("agent-b")
    err, text = call(b, "wp_add", title="Production release", track="release", waits_on=["W-1"])
    assert "waits on W-1" in text
    err, text = call(a, "wp_mine")
    assert not err
    assert "W-1 [next] Release notes for 2.4" in text and "waited on by: W-2 Production release (agent-b)" in text
    assert "Next 24 hours:" in text
    err, text = call(a, "wp_done", id="W-1", note="published")
    assert text == "Done W-1 Release notes for 2.4. Now free to start (owners told): W-2."
    assert store.get_item("W-2")["state"] == "next"
    err, text = call(a, "wp_mine")
    assert text == "No open items for agent-a."


def test_update_and_clearing(mcp_for, store):
    a = mcp_for("agent-a")
    call(a, "wp_add", title="Soak", track="release", note="starting", wake="2026-03-10T15:00:00+01:00")
    err, text = call(a, "wp_update", id="W-1", state="running", note="no errors yet")
    assert not err and "[running]" in text and "no errors yet" in text
    err, text = call(a, "wp_update", id="W-1", note="", wake="")
    it = store.get_item("W-1")
    assert it["note"] is None and it["wake_at"] is None
    err, text = call(a, "wp_update", id="W-1")
    assert err and "nothing to change" in text


def test_ask_a_human(mcp_for, store):
    b = mcp_for("agent-b")
    call(b, "wp_add", title="Migration tool", track="infra")
    err, text = call(b, "wp_ask", human="alice", title="Choose the migration strategy",
                     question="Online or in an offline window?", options=["online", "offline window"],
                     recommendation="online", for_item="W-1")
    assert not err and text == "Asked alice: W-2 Choose the migration strategy. W-1 waits on it."
    q = store.get_item("W-2")
    assert q["owner"] == "alice" and q["kind"] == "decision" and q["track"] == "infra"
    assert q["question"]["recommendation"] == "online"
    assert store.get_item("W-1")["state"] == "waiting"
    err, text = call(mcp_for("alice"), "wp_list", waiting_on="alice")
    assert text.startswith("W-1 [waiting] Migration tool")
    err, text = call(mcp_for("alice"), "wp_get", id="W-2")
    assert "question: Online or in an offline window?" in text and "options: online | offline window" in text


def test_the_daemon_enforces_the_rules(mcp_for):
    a = mcp_for("agent-a")
    call(a, "wp_add", title="Mine", track="release")
    err, text = call(mcp_for("agent-b"), "wp_update", id="W-1", note="not yours")
    assert err and text.endswith("forbidden: an agent changes only its own items")
    call(a, "wp_add", title="Second", track="release", waits_on=["W-1"])
    err, text = call(a, "wp_update", id="W-1", add_waits_on=["W-2"])
    assert err and text.endswith("loop: the conditions would close a loop: W-1 → W-2 → W-1")
    err, text = call(a, "wp_add", title="x" * 81, track="release")
    assert err and "invalid (title): title: 81 characters, at most 80" in text


def test_waypoint_down(tokens, tracks):
    server = build(WaypointClient("http://127.0.0.1:9", tokens["agent-a"], timeout=2), "agent-a")
    err, text = call(server, "wp_mine")
    assert err and text.endswith(": Waypoint unavailable")
    assert tokens["agent-a"] not in text


def test_input_conveniences():
    assert to_condition("W-12") == {"item": "W-12"}
    assert to_condition("2026-03-10T14:00:00Z") == {"time": "2026-03-10T14:00:00Z"}
    assert to_ts("2026-03-10T15:00:00+01:00") == "2026-03-10T14:00:00Z"
    local = to_ts("2026-03-10 14:00")  # no zone: this machine's zone
    assert local.endswith("Z") and len(local) == 20


def test_main_needs_a_name_and_a_token(monkeypatch, tmp_path, capsys):
    from waypoint import mcp_server

    monkeypatch.delenv("WAYPOINT_NAME", raising=False)
    assert mcp_server.main() == 2
    monkeypatch.setenv("WAYPOINT_NAME", "agent-a")
    monkeypatch.setenv("WAYPOINT_TOKEN_FILE", str(tmp_path / "missing.token"))
    assert mcp_server.main() == 2
    err = capsys.readouterr().err
    assert "WAYPOINT_NAME" in err and "cannot read the token file" in err and str(tmp_path) not in err


def test_ask_for_someone_elses_item_leaves_nothing_behind(mcp_for, store):
    call(mcp_for("agent-b"), "wp_add", title="Agent B's item", track="infra")
    err, text = call(mcp_for("agent-a"), "wp_ask", human="alice", title="Pick one", question="A or B?",
                     for_item="W-1")
    assert err and text.endswith("forbidden: an agent changes only its own items")
    assert [i["id"] for i in store.items()] == ["W-1"]  # no orphan decision on alice's list


def test_ask_twice_asks_once(mcp_for, store):
    b = mcp_for("agent-b")
    call(b, "wp_add", title="Migration tool", track="infra")
    for _ in range(2):
        err, text = call(b, "wp_ask", human="alice", title="Choose", question="Online?", for_item="W-1")
        assert text == "Asked alice: W-2 Choose. W-1 waits on it."
    assert len(store.items()) == 2


def test_done_names_who_is_free_and_who_still_waits(mcp_for):
    a = mcp_for("agent-a")
    call(a, "wp_add", title="A", track="release")
    call(a, "wp_add", title="B", track="release")
    call(mcp_for("agent-b"), "wp_add", title="Needs A", track="release", waits_on=["W-1"])
    call(mcp_for("agent-c"), "wp_add", title="Needs A and B", track="release", waits_on=["W-1", "W-2"])
    err, text = call(a, "wp_done", id="W-1", note="done")
    assert text == "Done W-1 A. Now free to start (owners told): W-3. Still waiting on something else: W-4."


@pytest.mark.parametrize("bad", ["W-1/../../v1/clients", "W-1?state=all", "12", "W-1#x"])
def test_ids_are_checked_before_they_go_into_a_url(mcp_for, bad):
    err, text = call(mcp_for("agent-a"), "wp_get", id=bad)
    assert err and "not an item id" in text
