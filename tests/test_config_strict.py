"""Configurations are checked strictly: a typo, a wrong type or a ladder step that can't work is
refused at load with a clear message, never loaded silently or as a traceback. `waypointd check`
and `waypoint-deliver --check` check a configuration without changing anything. The content
check refuses UUIDs and MAC addresses."""

from __future__ import annotations

import sqlite3

import pytest

from waypoint import config, content, db, deliver, server
from waypoint.adapters import PushAdapter
from waypoint.store import Settings, Store


def load(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text)
    return config.load(p)


# ---------------------------------------------------------------- waypointd (CFG-1..6, API-7, API-8)
@pytest.mark.parametrize("text, message", [
    ("[server]\nprot = 9000\n", "unknown key: server.prot"),
    ('[site]\ncordinator = "x"\n', "unknown key: site.cordinator"),
    ('[store]\npath = "x"\n', "unknown key: store.path"),
    ("server = 1\n", "server: a [server] table"),
    ("[server]\nport = true\n", "server.port"),
    ('[server]\nport = "8080"\n', "server.port"),
    ("[server]\nport = 0\n", "server.port"),
    ('[server]\nlisten = "127.0.0.1"\n', "server.listen: a list"),
    ("[server]\nlisten = [123]\n", "server.listen: a list"),
    ('[server]\nlisten = ["127.0.0.1", "127.0.0.1"]\n', "listed twice"),
    ('[store]\ndatabase = ""\n', "store.database"),
    ("[site]\ndone_days = -5\n", "site.done_days"),
    ("[site]\ntz = 3\n", "site.tz"),
    ('[site]\ndefault_ladder = "x"\n', "site.default_ladder: a list"),
    ("[site]\ndefault_ladder = [1]\n", "site.default_ladder[0]"),
    ('[site]\ndefault_ladder = [{ action = "nudge" }]\n', "site.default_ladder[0]: not a valid"),
    ('[site]\ndefault_ladder = [{ after = "soon", action = "nudge" }]\n', "site.default_ladder[0]: not a valid"),
    ('[site]\ndefault_ladder = [{ after = "1h", action = "notify" }]\n', 'site.default_ladder[0]: "to"'),
    ('[site]\ndefault_ladder = [{ after = "1h", action = "nudge", to = "alice" }]\n', 'site.default_ladder[0]: "to"'),
])
def test_bad_daemon_configs_are_refused_by_name(tmp_path, text, message):
    with pytest.raises(config.ConfigError) as e:
        load(tmp_path, text)
    assert message in str(e.value)


def test_good_values_still_load(tmp_path):
    cfg = load(tmp_path, '[server]\nlisten = ["127.0.0.1", "::1"]\nport = 8080\n'
                         '[site]\ndone_days = 0\ndefault_ladder = [{ after = "0m", action = "notify", to = "alice" }]\n')
    assert cfg.port == 8080 and cfg.settings.done_days == 0
    assert cfg.settings.default_ladder == [{"after": "0m", "action": "notify", "to": "alice"}]


def test_check_clients():
    s = Settings(coordinator="coordinator",
                 default_ladder=[{"after": "1h", "action": "notify", "to": "alice"}])
    config.check_clients(s, {"coordinator": "coordinator", "alice": "human"})
    with pytest.raises(config.ConfigError, match="site.coordinator"):
        config.check_clients(s, {"alice": "human"})
    with pytest.raises(config.ConfigError, match=r"site.default_ladder\[0\].to"):
        config.check_clients(s, {"coordinator": "coordinator", "alice": "agent"})


def _config_with_db(tmp_path, site=""):
    cfgp = tmp_path / "c.toml"
    cfgp.write_text(f'[store]\ndatabase = "{tmp_path / "w.db"}"\n[site]\n{site}')
    return cfgp


def test_serve_refuses_a_ladder_that_notifies_nobody(tmp_path, capsys):
    cfgp = _config_with_db(tmp_path, 'default_ladder = [{ after = "1h", action = "notify", to = "alice" }]\n')
    assert server.main(["--config", str(cfgp), "serve"]) == 2
    assert "is not a registered human" in capsys.readouterr().err


def test_a_malformed_ladder_is_a_clean_error(tmp_path, capsys):
    cfgp = _config_with_db(tmp_path, 'default_ladder = "x"\n')
    assert server.main(["--config", str(cfgp), "backup", str(tmp_path / "b")]) == 2
    err = capsys.readouterr().err
    assert err.startswith("waypointd: configuration: site.default_ladder") and "Traceback" not in err


def test_waypointd_check(tmp_path, capsys):
    cfgp = _config_with_db(tmp_path, 'coordinator = "coordinator"\n')
    assert server.main(["--config", str(cfgp), "check"]) == 1  # no database yet
    assert "cannot read the database" in capsys.readouterr().err
    assert server.main(["--config", str(cfgp), "check", "--config-only"]) == 0
    st = Store(db.connect(tmp_path / "w.db"))
    st.add_client("agent-a", "agent")
    st.conn.close()
    assert server.main(["--config", str(cfgp), "check"]) == 2
    assert "not a registered coordinator" in capsys.readouterr().err
    st = Store(db.connect(tmp_path / "w.db"))
    st.add_client("coordinator", "coordinator")
    st.conn.close()
    assert server.main(["--config", str(cfgp), "check"]) == 0
    assert "configuration OK, 2 live clients" in capsys.readouterr().out


def test_waypointd_check_reads_an_older_schema_without_migrating(tmp_path, capsys):
    path = tmp_path / "w.db"
    conn = sqlite3.connect(path)
    db.migrate(conn, db.MIGRATIONS[:5])
    conn.execute("INSERT INTO clients (name, role, label, created_at) VALUES ('coordinator', 'coordinator', 'C', "
                 "'2026-03-10T09:00:00Z')")
    conn.commit()
    conn.close()
    cfgp = _config_with_db(tmp_path, 'coordinator = "coordinator"\n')
    assert server.main(["--config", str(cfgp), "check"]) == 0
    assert f"migrates it to {len(db.MIGRATIONS)} at start" in capsys.readouterr().out
    conn = sqlite3.connect(path)
    assert db.schema_version(conn) == 5
    conn.close()


# ---------------------------------------------------------------- waypoint-deliver (C3, strict keys)
def _deliver_config(tmp_path, body):
    tok = tmp_path / "host-1.token"
    tok.write_text("t" * 43 + "\n")
    cfg = tmp_path / "d.toml"
    cfg.write_text(f'[waypoint]\ntoken_file = "{tok}"\n{body}')
    return cfg


@pytest.mark.parametrize("body, message", [
    ('tokenfile = "x"\n', "unknown key: waypoint.tokenfile"),
    ('[recipients.alice]\nadapter = "push"\n', "unknown section: recipients"),
    ('[recipient.alice]\nadapter = "push"\nurl = "https://push.example.org/t"\ntitel = "W"\n', "unknown key titel"),
    ('[recipient.agent-a]\nadapter = "tmux"\ntarget = "main"\nsoket = "a"\n', "unknown key soket"),
    ('[recipient.alice]\nadapter = "push"\nurl = "https://push.example.org/t"\ntitle = "Waypoint ✅"\n', "Latin-1"),
    ('[recipient.alice]\nadapter = "push"\nurl = "https://push.example.org/t"\npriority = "hoch ✅"\n', "Latin-1"),
])
def test_bad_deliverer_configs_are_refused_by_name(tmp_path, body, message):
    with pytest.raises(deliver.ConfigError) as e:
        deliver.load(str(_deliver_config(tmp_path, body)))
    assert message in str(e.value)


def test_latin1_push_title_loads_and_priority_is_one_line(tmp_path):
    _, adapters = deliver.load(str(_deliver_config(
        tmp_path, '[recipient.alice]\nadapter = "push"\nurl = "https://push.example.org/t"\ntitle = "Waypoint é"\n')))
    assert isinstance(adapters["alice"], PushAdapter)
    sent = {}
    adapter = PushAdapter("https://push.example.org/t", priority="high\nX-Evil: 1")
    import waypoint.adapters as ad
    orig = ad._post
    ad._post = lambda url, body, headers, timeout: sent.update(headers) or "delivered"
    try:
        adapter.deliver({"summary": "s", "text": "", "id": "D-1"})
    finally:
        ad._post = orig
    assert sent["Priority"] == "high X-Evil: 1"


def test_deliverer_check(tmp_path, capsys):
    cfg = _deliver_config(tmp_path, '[recipient.agent-a]\nadapter = "tmux"\ntarget = "main"\n')
    assert deliver.main(["--config", str(cfg), "--check"]) == 0
    assert "configuration OK, 1 recipient(s)" in capsys.readouterr().out


# ---------------------------------------------------------------- the content check (S10)
@pytest.mark.parametrize("text", [
    "session 3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b restarted",
    "device AA:BB:CC:DD:EE:FF on the switch",
    "mac aa-bb-cc-dd-ee-ff",
])
def test_uuids_and_macs_are_refused(text):
    assert content.problem(text)


@pytest.mark.parametrize("text", [
    "released 2026-11-20 at 12:30:45", "build 1-2-3-4-5-6", "ratio 16:9:4:3", "W-12 waits on W-17",
    "version 1.2.3-rc1", "a1b2-c3d4", "deadbeef-dead",
])
def test_ordinary_text_still_passes(text):
    assert not content.problem(text)


def test_a_uuid_may_name_a_page_in_a_link():
    assert not content.link_problem("https://docs.example.org/d/3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b/edit")
    assert content.link_problem("https://example.org/x?session=3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b")
    assert content.link_problem("https://example.org/aa:bb:cc:dd:ee:ff")
