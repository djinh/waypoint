"""wp against a real server on 127.0.0.1: items, ask, tracks, clients, exit codes, no tokens printed."""

from __future__ import annotations

import io
import json
import stat

import pytest

from waypoint import wp


@pytest.fixture
def run(server, tokens, tmp_path, monkeypatch):
    for k in ("WAYPOINT_URL", "WAYPOINT_TOKEN_FILE", "WAYPOINT_NAME"):
        monkeypatch.delenv(k, raising=False)

    def go(who, *argv):
        tf = tmp_path / f"{who}.token"
        if not tf.exists():
            tf.write_text(tokens[who] + "\n")
        out, err = io.StringIO(), io.StringIO()
        code = wp.main(["--url", server.base, "--token-file", str(tf), "--name", who, *argv], out, err)
        text = out.getvalue() + err.getvalue()
        for t in tokens.values():
            assert t not in text
        return code, out.getvalue().strip(), err.getvalue().strip()

    return go


def test_add_ls_done(run, tracks):
    code, out, _ = run("agent-a", "add", "Release notes for 2.4", "--track", "release", "--due",
                       "2026-03-10T11:00:00Z", "--note", "draft")
    assert code == 0 and out.startswith("W-1 [next] Release notes for 2.4 (agent-a, release)")
    run("agent-b", "add", "Production release", "--track", "release", "--waits-on", "W-1")
    code, out, _ = run("alice", "ls", "--track", "release")
    assert out.splitlines()[1].startswith("W-2 [waiting] Production release") and "waits on W-1" in out
    code, out, _ = run("agent-a", "mine")
    assert out.startswith("W-1 [next]")
    code, out, _ = run("agent-a", "--json", "done", "W-1", "released 14:02")
    assert code == 0 and json.loads(out)["state"] == "done"
    code, out, _ = run("agent-b", "ls", "--state", "next")
    assert out.startswith("W-2 [next]")


def test_update_drop_show(run, tracks):
    run("agent-a", "add", "Hotfix", "--track", "release", "--wake", "2026-03-10T12:00:00Z")
    code, out, _ = run("agent-a", "update", "W-1", "--state", "running", "--note", "building", "--wake", "")
    assert code == 0 and "[running]" in out and "wake" not in out
    code, out, _ = run("agent-a", "drop", "W-1", "folded into 2.4")
    assert "[dropped]" in out
    code, out, _ = run("alice", "show", "W-1")
    assert "agent-a changed: folded into 2.4" in out


def test_ask(run, tracks, store):
    run("agent-b", "add", "Migration tool", "--track", "infra")
    for _ in range(2):
        code, out, _ = run("agent-b", "ask", "alice", "Choose the strategy", "Online or offline?",
                           "--option", "online", "--option", "offline", "--recommend", "online", "--for", "W-1")
        assert code == 0 and out.endswith("W-1 waits on it.")
    assert len(store.items()) == 2
    code, out, _ = run("alice", "ls", "--waiting-on", "alice")
    assert out.startswith("W-1 [waiting] Migration tool")


def test_exit_codes(run, tracks, server, tmp_path):
    code, _, err = run("agent-a", "update", "W-99", "--note", "x")
    assert code == 1 and err == "wp: not_found: no such item"
    code, _, err = run("agent-a", "add", "x", "--track", "release", "--due", "next week")
    assert code == 2 and "not a time" in err
    code, _, err = run("agent-a", "show", "W-1/../clients")
    assert code == 2 and "not an item id" in err
    run("agent-a", "add", "A", "--track", "release")
    run("agent-a", "add", "B", "--track", "release", "--waits-on", "W-1")
    code, _, err = run("agent-a", "update", "W-1", "--add-waits-on", "W-2")
    assert code == 1 and err == "wp: loop: the conditions would close a loop: W-1 → W-2 → W-1"
    out, errs = io.StringIO(), io.StringIO()
    tf = tmp_path / "agent-a.token"
    assert wp.main(["--url", "http://127.0.0.1:9", "--token-file", str(tf), "ls"], out, errs) == 3
    assert errs.getvalue().strip() == "wp: Waypoint unavailable"
    assert wp.main(["--url", server.base, "ls"], out, errs) == 2  # no token


def test_tracks(run):
    code, out, _ = run("agent-a", "track", "put", "release", "--title", "Release 2.4", "--lead", "agent-a")
    assert code == 0 and out == "release: Release 2.4"
    code, out, _ = run("agent-b", "track", "put", "release", "--title", "Mine now")
    assert code == 1
    code, out, _ = run("pages", "tracks")
    assert out == "release: Release 2.4 (lead agent-a)"


def test_clients(run, tmp_path, server):
    newfile = tmp_path / "agent-d.token"
    code, out, err = run("coordinator", "client", "add", "agent-d", "--role", "agent", "--deliverer", "host-1",
                         "--token-file", str(newfile))
    # stdout stays empty: the status goes to stderr, so a script capturing stdout gets nothing extra
    assert code == 0 and out == "" and err.splitlines()[0] == "agent-d (agent) · via host-1"
    assert stat.S_IMODE(newfile.stat().st_mode) == 0o600
    token = newfile.read_text().strip()
    assert token not in out
    code, _, err = run("coordinator", "client", "add", "agent-e", "--role", "agent", "--token-file", str(newfile))
    assert code == 2 and "exists already" in err
    code, out, _ = run("agent-a", "client", "ls")
    assert code == 1
    rotated = tmp_path / "agent-d.new"
    code, out, err = run("alice", "client", "rotate", "agent-d", "--token-file", str(rotated))
    assert code == 0 and rotated.read_text().strip() != token
    assert out == "" and "token written to" in err
    code, out, _ = run("alice", "--json", "client", "rotate", "agent-d", "--token-file", str(tmp_path / "j"))
    assert code == 0 and json.loads(out)["name"] == "agent-d" and "token" not in json.loads(out)
    code, out, _ = run("alice", "client", "route", "agent-d", "-")
    assert out == "agent-d (agent)"
    code, out, _ = run("alice", "client", "retire", "agent-d")
    assert out.endswith("retired")
    code, out, _ = run("coordinator", "client", "ls")
    assert "agent-d (agent) · retired" in out


def test_health(server):
    out = io.StringIO()
    assert wp.main(["--url", server.base, "health"], out, io.StringIO()) == 0
    assert out.getvalue().startswith("ok ")
