"""SQLite: connection settings and numbered migrations.

Migrations run at start, each in its own transaction, in order. An applied migration is never
edited; a schema change is a new entry at the end of MIGRATIONS.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: clients, tracks, items, conditions, history, ladder steps, scheduler passes
    """
    CREATE TABLE clients (
        name        TEXT PRIMARY KEY,
        role        TEXT NOT NULL CHECK (role IN ('agent','human','coordinator','reader','deliverer')),
        label       TEXT,
        token_hash  TEXT UNIQUE,
        deliverer   TEXT REFERENCES clients(name),
        created_at  TEXT NOT NULL,
        retired_at  TEXT
    );
    CREATE TABLE tracks (
        id        TEXT PRIMARY KEY,
        title     TEXT NOT NULL,
        lead      TEXT,
        note      TEXT,
        ord       INTEGER NOT NULL DEFAULT 0,
        archived  INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE items (
        num            INTEGER PRIMARY KEY AUTOINCREMENT,
        owner          TEXT NOT NULL,
        track          TEXT NOT NULL REFERENCES tracks(id),
        title          TEXT NOT NULL,
        label          TEXT,
        kind           TEXT NOT NULL CHECK (kind IN ('task','decision','approval','event')),
        state          TEXT NOT NULL CHECK (state IN ('next','running','waiting','done','dropped')),
        state_since    TEXT NOT NULL,
        question       TEXT,
        when_start     TEXT,
        when_end       TEXT,
        when_precision TEXT CHECK (when_precision IN ('minute','hour','day')),
        due_at         TEXT,
        wake_at        TEXT,
        overdue        TEXT NOT NULL DEFAULT '[]',
        note           TEXT,
        links          TEXT NOT NULL DEFAULT '[]',
        late           INTEGER NOT NULL DEFAULT 0,
        at_risk_from   TEXT NOT NULL DEFAULT '[]',
        repeat_rule    TEXT,
        version        INTEGER NOT NULL DEFAULT 1,
        created_at     TEXT NOT NULL,
        created_by     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        updated_by     TEXT,
        done_at        TEXT
    );
    CREATE INDEX items_owner ON items(owner, state);
    CREATE INDEX items_track ON items(track);
    CREATE INDEX items_state ON items(state);
    CREATE TABLE conditions (
        item     INTEGER NOT NULL REFERENCES items(num),
        pos      INTEGER NOT NULL,
        dep      INTEGER REFERENCES items(num),
        at       TEXT,
        met_at   TEXT,
        PRIMARY KEY (item, pos),
        CHECK ((dep IS NULL) != (at IS NULL))
    );
    CREATE INDEX conditions_dep ON conditions(dep);
    CREATE TABLE history (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        item     INTEGER NOT NULL REFERENCES items(num),
        at       TEXT NOT NULL,
        by       TEXT,
        event    TEXT NOT NULL,
        changes  TEXT,
        note     TEXT
    );
    CREATE INDEX history_item ON history(item, id);
    CREATE TABLE ladder_steps (
        item       INTEGER NOT NULL REFERENCES items(num),
        due_at     TEXT NOT NULL,
        step       INTEGER NOT NULL,
        action     TEXT NOT NULL,
        target     TEXT,
        at         TEXT NOT NULL,
        caught_up  INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (item, due_at, step)
    );
    CREATE TABLE passes (
        id  INTEGER PRIMARY KEY AUTOINCREMENT,
        at  TEXT NOT NULL
    );
    """,
    # 2: messages (created when they fall due), wake-ups handled, skipped ladder steps
    """
    CREATE TABLE messages (
        num          INTEGER PRIMARY KEY AUTOINCREMENT,
        recipient    TEXT NOT NULL,
        kind         TEXT NOT NULL,
        item         INTEGER REFERENCES items(num),
        items        TEXT NOT NULL DEFAULT '[]',
        fell_due_at  TEXT NOT NULL,
        late         TEXT,
        text         TEXT NOT NULL,
        summary      TEXT NOT NULL,
        links        TEXT NOT NULL DEFAULT '[]',
        created_at   TEXT NOT NULL,
        withdrawn_at TEXT
    );
    CREATE INDEX messages_recipient ON messages(recipient, num);
    CREATE INDEX messages_item ON messages(item);
    CREATE TABLE wakes (
        item     INTEGER NOT NULL REFERENCES items(num),
        wake_at  TEXT NOT NULL,
        outcome  TEXT NOT NULL CHECK (outcome IN ('sent','dropped','cancelled')),
        at       TEXT NOT NULL,
        PRIMARY KEY (item, wake_at)
    );
    ALTER TABLE ladder_steps ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0
    """,
    # 3: delivery state of messages, deliverers' last contact
    """
    ALTER TABLE messages ADD COLUMN handed_at TEXT;
    ALTER TABLE messages ADD COLUMN lease_until TEXT;
    ALTER TABLE messages ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE messages ADD COLUMN acked_at TEXT;
    ALTER TABLE messages ADD COLUMN ack_result TEXT;
    ALTER TABLE messages ADD COLUMN ack_detail TEXT;
    CREATE INDEX messages_pending ON messages(acked_at, withdrawn_at, recipient);
    CREATE TABLE deliverers (
        name                TEXT PRIMARY KEY REFERENCES clients(name),
        last_seen           TEXT,
        silent_reported_at  TEXT
    )
    """,
    # 4: where a decision or approval is answered ({"issue": url} or {"session": name}, as JSON)
    """
    ALTER TABLE items ADD COLUMN answer_at TEXT
    """,
    # 5: the running lease: until when, the check message sent when it passed, when it lapses.
    # Items already running get their lease at the first scheduler pass (now + the configured lease).
    """
    ALTER TABLE items ADD COLUMN running_until TEXT;
    ALTER TABLE items ADD COLUMN running_check INTEGER;
    ALTER TABLE items ADD COLUMN running_lapse_at TEXT
    """,
]


class SchemaMismatch(RuntimeError):
    pass


def connect(path: str | Path, migrate_schema: bool = True) -> sqlite3.Connection:
    """Open the database with Waypoint's settings and bring its schema up to date.

    With migrate_schema=False (tools that run beside a live daemon: backup, local admin commands)
    the schema is left alone, and a schema other than this code's is refused, so a tool never
    migrates a database under a running daemon of another version. A brand-new, empty database is
    still set up, so the first clients can be registered before the daemon's first start."""
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    if migrate_schema or schema_version(conn) == 0:
        migrate(conn)  # a brand-new database has nothing a running daemon could depend on
    elif schema_version(conn) != len(MIGRATIONS):
        found = schema_version(conn)
        conn.close()
        raise SchemaMismatch(f"the database has schema {found}, this Waypoint expects {len(MIGRATIONS)}; "
                             "start the daemon of this version first (it migrates), or use the matching version")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, migrations: list[str] | None = None) -> int:
    """Apply the migrations not applied yet; returns the schema version."""
    migrations = MIGRATIONS if migrations is None else migrations
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = schema_version(conn)
        if current > len(migrations):
            raise RuntimeError(f"database schema {current} is newer than this Waypoint ({len(migrations)})")
        for n in range(current, len(migrations)):
            for stmt in _statements(migrations[n]):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {n + 1}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return schema_version(conn)


def _statements(script: str) -> list[str]:
    """Split a migration into statements (no semicolons inside our statements)."""
    return [s.strip() for s in script.split(";") if s.strip()]
