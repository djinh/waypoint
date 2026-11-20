"""Smaller fixes: escalations without a coordinator are logged, views handle chains of any depth,
hour precision follows the deployment's zone, a track's lead is a writer, `wp` can remove single
conditions and keeps a track's archived state, `--json` errors are JSON, and message kinds are one
fixed set."""

from __future__ import annotations

import io
import itertools
import json
import logging

import pytest

from waypoint import wp
from waypoint.adapters import _REF_KIND, one_line
from waypoint.clock import FakeClock
from waypoint.db import connect
from waypoint.deliveries import WITHDRAW_ON_CHANGE
from waypoint.errors import ApiError
from waypoint.messages import KINDS, Outbox
from waypoint.scheduler import Scheduler
from waypoint.store import Client, Settings, Store
from waypoint.views import _order

from conftest import T0


# ---------------------------------------------------------------- scheduler and views (C6, C7)
def test_an_escalation_without_a_coordinator_is_logged(tmp_path, caplog):
    st = Store(connect(tmp_path / "w.db"), FakeClock(T0), Settings(tz="UTC"))
    st.settings.outage_seconds = 10**9
    st.add_client("agent-a", "agent")
    a = Client("agent-a", "agent", None)
    st.put_track(a, "t", {"title": "T"})
    st.create_item(a, {"track": "t", "title": "late", "kind": "task", "due_at": "2026-03-10T09:00:00Z"})
    sched = Scheduler(st)
    sched.run_pass()
    with caplog.at_level(logging.WARNING, logger="waypoint.scheduler"):
        st.clock.advance(hours=2)
        sched.run_pass()
    assert "escalation for W-1 not sent: no coordinator" in caplog.text
    st.conn.close()


def test_views_order_a_diamond_in_every_order():
    # W-3 waits on W-4; W-2 waits on W-3; W-1 waits on W-3 and W-2: depths 0, 1, 2, 3
    items = {"W-4": {"track": "t", "waits_on": []},
             "W-3": {"track": "t", "waits_on": [{"item": "W-4"}]},
             "W-2": {"track": "t", "waits_on": [{"item": "W-3"}]},
             "W-1": {"track": "t", "waits_on": [{"item": "W-3"}, {"item": "W-2"}]}}
    for perm in itertools.permutations(items):
        for deps in ([{"item": "W-3"}, {"item": "W-2"}], [{"item": "W-2"}, {"item": "W-3"}]):
            items["W-1"]["waits_on"] = deps
            assert _order(items, list(perm), {"t": (0, "t")}) == ["W-4", "W-3", "W-2", "W-1"], (perm, deps)


def test_views_order_chains_of_any_depth():
    n = 5000
    items = {f"W-{i}": {"track": "t", "waits_on": [{"item": f"W-{i - 1}"}] if i > 1 else []}
             for i in range(1, n + 1)}
    order = _order(items, set(items), {"t": (0, "t")})
    assert order[0] == "W-1" and order[-1] == f"W-{n}" and len(order) == n


# ---------------------------------------------------------------- store (C8, API-9)
def test_hour_precision_is_cut_in_the_deployment_zone(tmp_path):
    st = Store(connect(tmp_path / "w.db"), FakeClock(T0), Settings(tz="Asia/Kathmandu"))
    st.add_client("agent-a", "agent")
    a = Client("agent-a", "agent", None)
    st.put_track(a, "t", {"title": "T"})
    it = st.create_item(a, {"track": "t", "title": "x", "kind": "event",
                            "when": {"start": "2026-03-10T14:30:00+05:45", "precision": "hour"}})
    assert it["when"]["start"] == "2026-03-10T08:15:00Z"  # 14:00 in Kathmandu
    st.conn.close()


@pytest.mark.parametrize("lead", ["pages", "host-1", "nobody"])
def test_a_track_lead_is_a_writer(store, who, tokens, lead):
    with pytest.raises(ApiError) as e:
        store.put_track(who("agent-a"), "t2", {"title": "T", "lead": lead})
    assert e.value.field == "lead"


# ---------------------------------------------------------------- wp (C16, QUAL-9)
@pytest.fixture
def run(server, tokens, tmp_path, monkeypatch):
    for k in ("WAYPOINT_URL", "WAYPOINT_TOKEN_FILE", "WAYPOINT_NAME"):
        monkeypatch.delenv(k, raising=False)

    def go(who, *argv):
        tf = tmp_path / f"{who}.token"
        tf.write_text(tokens[who] + "\n")
        tf.chmod(0o600)
        out, err = io.StringIO(), io.StringIO()
        code = wp.main(["--url", server.base, "--token-file", str(tf), "--name", who, *argv], out, err)
        return code, out.getvalue().strip(), err.getvalue().strip()

    return go


def test_remove_one_condition_and_clear_all(run, mk, store):
    mk("agent-a", "one")
    mk("agent-a", "two")
    mk("agent-a", "three", waits_on=[{"item": "W-1"}, {"item": "W-2"}, {"time": "2026-03-11T09:00:00Z"}])
    code, out, _ = run("agent-a", "update", "W-3", "--remove-waits-on", "W-1")
    assert code == 0
    assert [c.get("item") or c.get("time") for c in store.get_item("W-3")["waits_on"]] == [
        "W-2", "2026-03-11T09:00:00Z"]
    code, _, err = run("agent-a", "update", "W-3", "--remove-waits-on", "W-1")
    assert code == 2 and "doesn't wait on W-1" in err
    assert run("agent-a", "update", "W-3", "--waits-on", "")[0] == 0
    assert store.get_item("W-3")["waits_on"] == []


def test_wp_version_and_json_errors(run, capsys):
    with pytest.raises(SystemExit):
        wp.main(["--version"])
    assert capsys.readouterr().out.startswith("wp ")
    code, out, err = run("agent-a", "--json", "show", "W-99")
    assert code == 1 and out == ""
    assert json.loads(err) == {"error": {"code": "not_found", "message": "no such item"}}
    code, _, err = run("agent-a", "--json", "show", "nonsense")
    assert code == 2 and json.loads(err)["error"]["code"] == "usage"


def test_track_put_keeps_the_archived_state(run, store):
    assert run("alice", "track", "put", "ops", "--title", "Ops", "--archived")[0] == 0
    assert run("alice", "track", "put", "ops", "--title", "Operations")[0] == 0
    [t] = [t for t in store.tracks(archived=True) if t["id"] == "ops"]
    assert t["title"] == "Operations" and t["archived"]
    assert run("alice", "track", "put", "ops", "--title", "Operations", "--no-archived")[0] == 0
    assert not [t for t in store.tracks(archived=True) if t["id"] == "ops"][0]["archived"]


def test_json_output_escapes_line_separators(run, mk, store):
    mk("agent-a", "x")
    with store.tx() as c:  # a row from before control characters were refused
        c.execute("UPDATE items SET note = ? WHERE num = 1", ("a\u2028b",))
    code, out, _ = run("agent-a", "--json", "show", "W-1")
    assert code == 0 and " " not in out and "\\u2028" in out
    assert json.loads(out)["note"] == "a b"


def test_typed_lines_flatten_line_separators():
    assert one_line("a b c") == "a b c"


# ---------------------------------------------------------------- message kinds (QUAL-2)
def test_message_kinds_match_the_contract_and_the_adapters():
    from pathlib import Path

    import yaml
    root = Path(__file__).resolve().parent.parent
    spec = yaml.safe_load((root / "docs" / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    contract = spec["components"]["schemas"]["Delivery"]["properties"]["kind"]["enum"]
    assert list(KINDS) == contract
    assert set(_REF_KIND) == set(KINDS)
    assert set(WITHDRAW_ON_CHANGE) <= set(KINDS)


def test_an_unknown_message_kind_is_a_bug(store, tokens):
    with pytest.raises(ValueError):
        with store.tx() as c:
            Outbox.of(store).add(c, "agent-a", "wakeup", None, "x")
