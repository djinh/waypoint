"""Recurrences in the clients: the wp_recur MCP tool, wp_get R-n, the "Recurring" section of
wp_mine, occurrences marked (R-n), and the `wp recur` commands."""

from __future__ import annotations

import io
import json

import pytest

from waypoint import wp
from waypoint.client import WaypointClient
from waypoint.mcp_server import build
from waypoint.present import InputError, to_schedule

from test_mcp import call


@pytest.mark.parametrize("text, want", [
    ("every 15m", {"every": "15m"}),
    ("daily 09:00 tz Europe/Paris", {"daily": "09:00", "tz": "Europe/Paris"}),
    ("weekly mon,thu 06:40", {"weekly": ["mon", "thu"], "at": "06:40"}),
    ("monthly last 23:00", {"monthly": "last", "at": "23:00"}),
    ("monthly 1 04:30", {"monthly": 1, "at": "04:30"}),
])
def test_schedules_as_people_type_them(text, want):
    assert to_schedule(text) == want


@pytest.mark.parametrize("text", ["", "hourly", "daily", "monthly first 09:00", "every"])
def test_bad_schedule_text(text):
    with pytest.raises(InputError):
        to_schedule(text)


def test_the_mcp_tools(server, tokens, tracks, store):
    a = build(WaypointClient(server.base, tokens["agent-a"], timeout=10), "agent-a")
    err, text = call(a, "wp_recur", title="Morning review", track="release", schedule="daily 09:00")
    assert not err and text.startswith("R-1 [active] Morning review (agent-a, release) · daily 09:00 · next ")
    err, text = call(a, "wp_get", id="R-1")
    assert not err and "window 1h · overlap skip · catch-up latest" in text
    err, text = call(a, "wp_mine")
    assert "Recurring:" in text and "R-1 [active]" in text
    err, text = call(a, "wp_recur", id="R-1", state="paused")
    assert not err and "[paused]" in text and "next " not in text
    err, text = call(a, "wp_recur", title="x", track="release", schedule="hourly")
    assert err and "not a schedule" in text
    err, text = call(a, "wp_recur", title="Alice's", track="release", schedule="daily 09:00", owner="alice")
    assert err and "forbidden" in text


def test_an_occurrence_is_marked_with_its_recurrence(server, tokens, tracks, store, who):
    from waypoint.recurrences import Recurrences
    from waypoint.scheduler import Scheduler
    Recurrences.of(store).create(who("agent-a"), {"track": "release", "title": "Poll", "schedule": {"every": "15m"}})
    store.settings.outage_seconds = 10**9
    store.clock.advance(minutes=15)
    Scheduler(store).run_pass()
    err, text = call(build(WaypointClient(server.base, tokens["agent-a"], timeout=10), "agent-a"), "wp_mine")
    assert "W-1 [next] Poll (R-1) (agent-a, release)" in text


def test_wp_recur_commands(server, tokens, tracks, tmp_path, monkeypatch):
    for k in ("WAYPOINT_URL", "WAYPOINT_TOKEN_FILE", "WAYPOINT_NAME"):
        monkeypatch.delenv(k, raising=False)

    def run(who, *argv):
        tf = tmp_path / f"{who}.token"
        tf.write_text(tokens[who])
        tf.chmod(0o600)
        out, err = io.StringIO(), io.StringIO()
        code = wp.main(["--url", server.base, "--token-file", str(tf), "--name", who, *argv], out, err)
        return code, out.getvalue().strip(), err.getvalue().strip()

    code, out, _ = run("coordinator", "recur", "add", "Nightly backup", "--track", "release", "--owner", "coordinator",
                       "--schedule", "every 1d", "--checkin", "--checkin-by", "agent-c")
    assert code == 0 and out.startswith("R-1 [active] Nightly backup") and "check-ins by agent-c" in out
    assert run("agent-a", "recur", "ls")[1].startswith("R-1 [active]")
    code, out, _ = run("agent-c", "recur", "done", "R-1", "--note", "ok")
    assert code == 0 and out == "R-1: no open occurrence; recorded"
    assert run("agent-c", "recur", "pause", "R-1")[0] == 1  # the check-in client may only check in
    assert "[paused]" in run("coordinator", "recur", "pause", "R-1")[1]
    assert "[active]" in run("coordinator", "recur", "resume", "R-1")[1]
    assert json.loads(run("coordinator", "--json", "recur", "show", "R-1")[1])["checkin_by"] == "agent-c"
    assert "[stopped]" in run("coordinator", "recur", "stop", "R-1")[1]
    code, _, err = run("coordinator", "recur", "show", "W-1")
    assert code == 2 and "not a recurrence id" in err


@pytest.mark.parametrize("value", ["R-1/checkin", "R-²", "R-٣", "R-01", "R-1\nx", "W-1", "r-x"])
def test_recurrence_ids_are_checked_before_they_go_into_a_url(value):
    from waypoint.present import recurrence_id
    with pytest.raises(InputError):
        recurrence_id(value)


def test_ids_and_names_allow_no_trailing_newline():
    # typed values are stripped first (" W-12 " is fine); the patterns themselves end at the end
    from waypoint import present
    for rx, bad in ((present._ITEM, "W-1\n"), (present._RECURRENCE, "R-1\n"), (present._NAME, "agent-a\n")):
        assert rx.match(bad) is None
    assert present.item_id(" W-12 ") == "W-12" and present.recurrence_id(" r-3 ") == "R-3"


def test_the_mcp_tools_refuse_a_bad_recurrence_id(server, tokens, tracks):
    a = build(WaypointClient(server.base, tokens["agent-a"], timeout=10), "agent-a")
    for tool, args in (("wp_get", {"id": "R-1/checkin"}), ("wp_recur", {"id": "R-²", "note": "x"})):
        err, text = call(a, tool, **args)
        assert err and "not a recurrence id" in text


def test_wp_mine_works_against_a_waypoint_without_recurrences(server, tokens, tracks, monkeypatch):
    from waypoint.client import ApiFailure
    real = WaypointClient.request

    def older(self, method, path, *a, **kw):
        if path.startswith("/v1/recurrences"):
            raise ApiFailure(404, "not_found", "no such endpoint")
        return real(self, method, path, *a, **kw)

    monkeypatch.setattr(WaypointClient, "request", older)
    err, text = call(build(WaypointClient(server.base, tokens["agent-a"], timeout=10), "agent-a"), "wp_mine")
    assert not err and text == "No open items for agent-a."
