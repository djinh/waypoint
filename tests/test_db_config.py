"""SQLite settings and migrations; the configuration's safety checks; waypointd's local commands."""

from __future__ import annotations

import os
import stat
import sqlite3

import pytest

from waypoint import config, db, server


def test_connection_settings(tmp_path):
    conn = db.connect(tmp_path / "w.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
    assert db.schema_version(conn) == len(db.MIGRATIONS)


def test_migrations_run_once_and_in_order(tmp_path):
    conn = sqlite3.connect(tmp_path / "m.db", isolation_level=None)
    steps = ["CREATE TABLE a (x INTEGER)", "CREATE TABLE b (y INTEGER)"]
    assert db.migrate(conn, steps[:1]) == 1
    assert db.migrate(conn, steps) == 2
    assert db.migrate(conn, steps) == 2  # nothing re-applied
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"a", "b"} <= tables


def test_failed_migration_rolls_back(tmp_path):
    conn = sqlite3.connect(tmp_path / "f.db", isolation_level=None)
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(conn, ["CREATE TABLE a (x INTEGER); CREATE TABLE a (x INTEGER)"])
    assert db.schema_version(conn) == 0
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name = 'a'").fetchone()


def test_newer_schema_is_refused(tmp_path):
    conn = sqlite3.connect(tmp_path / "n.db", isolation_level=None)
    conn.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError):
        db.migrate(conn)


def test_config_defaults_are_safe():
    cfg = config.load(None)
    assert cfg.listen == ["127.0.0.1"]
    assert cfg.port == config.DEFAULT_PORT


@pytest.mark.parametrize("listen", ['["0.0.0.0"]', '["::"]', '["localhost"]', "[]"])
def test_config_refuses_wildcards(tmp_path, listen):
    p = tmp_path / "c.toml"
    p.write_text(f"[server]\nlisten = {listen}\n")
    with pytest.raises(config.ConfigError):
        config.load(p)


def test_config_example_loads(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = config.load(os.path.join(root, "config.example.toml"))
    assert cfg.listen == ["127.0.0.1"] and cfg.settings.done_days == 3


def test_config_checks_zone_and_ladder(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[site]\ntz = "Mars/Olympus"\n')
    with pytest.raises(config.ConfigError):
        config.load(p)
    p.write_text('[site]\ndefault_ladder = [{ after = "1h", action = "run" }]\n')
    with pytest.raises(config.ConfigError):
        config.load(p)


def test_client_add_writes_a_private_token_file(tmp_path, capsys):
    cfgp = tmp_path / "c.toml"
    cfgp.write_text(f'[store]\ndatabase = "{tmp_path / "w.db"}"\n')
    tok = tmp_path / "agent-a.token"
    assert server.main(["--config", str(cfgp), "client", "add", "agent-a", "agent", "--token-file", str(tok)]) == 0
    assert stat.S_IMODE(tok.stat().st_mode) == 0o600
    token = tok.read_text().strip()
    out = capsys.readouterr()
    assert token not in out.out and token not in out.err
    # the status line is on stderr; stdout stays empty for scripts that capture it
    assert out.out == "" and "token for agent-a written to" in out.err
    # refuses to overwrite, and rotation invalidates the old token
    assert server.main(["--config", str(cfgp), "client", "rotate", "agent-a", "--token-file", str(tok)]) == 1
    tok2 = tmp_path / "agent-a.token2"
    assert server.main(["--config", str(cfgp), "client", "rotate", "agent-a", "--token-file", str(tok2)]) == 0
    from waypoint.store import Store
    st = Store(db.connect(tmp_path / "w.db"))
    assert st.authenticate(token) is None
    assert st.authenticate(tok2.read_text().strip()).name == "agent-a"
    # a token file that can't be written costs the client nothing
    assert server.main(["--config", str(cfgp), "client", "rotate", "agent-a", "--token-file",
                        str(tmp_path / "missing-dir" / "t")]) == 1
    assert st.authenticate(tok2.read_text().strip()).name == "agent-a"
    # tokens are stored only as hashes
    raw = (tmp_path / "w.db").read_bytes()
    assert tok2.read_text().strip().encode() not in raw


def test_serve_refuses_an_unknown_coordinator(tmp_path, capsys):
    cfgp = tmp_path / "c.toml"
    cfgp.write_text(f'[store]\ndatabase = "{tmp_path / "w.db"}"\n[site]\ncoordinator = "coordinatr"\n')
    assert server.main(["--config", str(cfgp), "serve"]) == 2
    assert "is not a registered coordinator" in capsys.readouterr().err
    cfgp.write_text(f'[store]\ndatabase = "{tmp_path / "w.db"}"\n[site]\ncoordinator = "agent-a"\n')
    assert server.main(["--config", str(cfgp), "client", "add", "agent-a", "agent", "--token-file",
                        str(tmp_path / "t")]) == 0
    assert server.main(["--config", str(cfgp), "serve"]) == 2


def test_client_route(tmp_path, capsys):
    cfgp = tmp_path / "c.toml"
    cfgp.write_text(f'[store]\ndatabase = "{tmp_path / "w.db"}"\n')
    run = lambda *a: server.main(["--config", str(cfgp), *a])  # noqa: E731
    assert run("client", "add", "host-1", "deliverer", "--token-file", str(tmp_path / "h")) == 0
    assert run("client", "add", "agent-a", "agent", "--token-file", str(tmp_path / "a"), "--deliverer", "host-1") == 0
    assert run("client", "add", "agent-b", "agent", "--token-file", str(tmp_path / "b"), "--deliverer", "agent-a") == 1
    assert not (tmp_path / "b").exists()
    assert run("client", "add", "agent-b", "agent", "--token-file", str(tmp_path / "b")) == 0
    capsys.readouterr()
    assert run("client", "route", "agent-b", "host-1") == 0
    out = capsys.readouterr()
    assert out.out == "" and "reached through host-1" in out.err
    assert run("client", "route", "agent-b", "nobody") == 1
    from waypoint.store import Store
    st = Store(db.connect(tmp_path / "w.db"))
    assert st.deliverer_of("agent-a") == "host-1" and st.deliverer_of("agent-b") == "host-1"
    assert run("client", "route", "agent-b", "-") == 0
    assert Store(db.connect(tmp_path / "w.db")).deliverer_of("agent-b") is None


def test_tools_beside_a_daemon_never_migrate(tmp_path):
    p = tmp_path / "w.db"
    conn = sqlite3.connect(p, isolation_level=None)
    db.migrate(conn, db.MIGRATIONS[:1])  # an older daemon's database
    conn.close()
    with pytest.raises(db.SchemaMismatch):
        db.connect(p, migrate_schema=False)
    assert db.schema_version(sqlite3.connect(p)) == 1  # untouched
    fresh = db.connect(tmp_path / "new.db", migrate_schema=False)  # a brand-new one is set up
    assert db.schema_version(fresh) == len(db.MIGRATIONS)


def test_backup_command_refuses_another_schema(tmp_path, capsys):
    p = tmp_path / "w.db"
    conn = sqlite3.connect(p, isolation_level=None)
    db.migrate(conn, db.MIGRATIONS[:2])
    conn.close()
    cfgp = tmp_path / "c.toml"
    cfgp.write_text(f'[store]\ndatabase = "{p}"\n')
    assert server.main(["--config", str(cfgp), "backup", str(tmp_path / "b")]) == 1
    assert "expects" in capsys.readouterr().err
    assert not (tmp_path / "b").exists()
