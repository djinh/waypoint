"""waypointd: the daemon, and local admin commands that work on its database directly.

    waypointd [--config FILE] serve
    waypointd [--config FILE] client add NAME ROLE --token-file FILE [--label TEXT]
    waypointd [--config FILE] client rotate NAME --token-file FILE
    waypointd [--config FILE] client route NAME DELIVERER|-
    waypointd [--config FILE] backup DIR [--keep-days N]
    waypointd [--config FILE] check [--config-only]

Tokens are written only to the named file, created with mode 0600; they are never printed. The
client commands print their status line on stderr and nothing on stdout.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sqlite3
import sys

from . import __version__
from .config import ConfigError, check_clients, load
from .db import MIGRATIONS, SchemaMismatch, connect
from .errors import ApiError
from .store import ALL_ROLES, Store


def issue_token(path: str, make_token) -> None:
    """Create the token file (mode 0600 from the start, never over an existing file) *before*
    ``make_token()`` changes the database, so a file that can't be written never costs a client
    its working token. On any failure the new file is removed."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(make_token() + "\n")
    except BaseException:
        os.unlink(path)
        raise


class BindError(OSError):
    pass


def bind_sockets(addresses: list[str], port: int) -> list[socket.socket]:
    """One listening socket per address; if any address can't be bound, none stays open."""
    socks = []
    for addr in addresses:
        family = socket.AF_INET6 if ":" in addr else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        socks.append(s)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((addr, port))
            s.listen(128)
        except OSError as e:
            for t in socks:
                t.close()
            raise BindError(f"cannot listen on {addr} port {port}: {e.strerror}") from None
        s.setblocking(False)
    return socks


def live_clients(conn: sqlite3.Connection) -> dict[str, str]:
    """Each unretired client's role, by name (the clients table is the same in every schema)."""
    return {r[0]: r[1] for r in conn.execute("SELECT name, role FROM clients WHERE retired_at IS NULL")}


def check_coordinator(store: Store) -> None:
    """The clients the configuration names must exist with the right role, or escalations and
    notifications go nowhere."""
    with store.read() as c:
        check_clients(store.settings, live_clients(c))


def dry_check(cfg, config_only: bool) -> int:
    """`waypointd check`: the configuration loads (done by the caller) and, unless CONFIG_ONLY, the
    clients it names exist in the database, which is opened read-only and never migrated."""
    if config_only:
        print("waypointd: configuration OK (clients not checked)")
        return 0
    try:
        conn = sqlite3.connect(f"file:{cfg.database}?mode=ro", uri=True)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            clients = live_clients(conn)
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"waypointd: check: cannot read the database: {e}", file=sys.stderr)
        return 1
    try:
        check_clients(cfg.settings, clients)
    except ConfigError as e:
        print(f"waypointd: configuration: {e}", file=sys.stderr)
        return 2
    note = "" if version == len(MIGRATIONS) else (
        f"; the database has schema {version}, this version migrates it to {len(MIGRATIONS)} at start"
        if version < len(MIGRATIONS) else f"; the database has schema {version}, newer than this version's "
                                          f"{len(MIGRATIONS)}: it would refuse to start")
    print(f"waypointd: configuration OK, {len(clients)} live clients{note}")
    return 0 if version <= len(MIGRATIONS) else 1


def serve(cfg) -> None:
    import uvicorn

    from .api import create_app
    from .scheduler import Scheduler

    store = Store(connect(cfg.database), settings=cfg.settings)
    check_coordinator(store)
    app = create_app(store, scheduler=Scheduler(store))
    # open event streams would hold a stop forever: give them a few seconds, then close them
    # limit_concurrency: beyond this many open connections and tasks, uvicorn answers 503 at once
    server = uvicorn.Server(uvicorn.Config(app, log_level="info", proxy_headers=False, server_header=False,
                                           timeout_graceful_shutdown=5, limit_concurrency=500))
    server.run(sockets=bind_sockets(cfg.listen, cfg.port))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="waypointd", description="Waypoint daemon")
    p.add_argument("--config", help="TOML configuration file")
    p.add_argument("--version", action="version", version=f"waypointd {__version__}")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the daemon (the default)")
    bk = sub.add_parser("backup", help="write a consistent copy of the database and a JSON export to DIR")
    bk.add_argument("dir")
    bk.add_argument("--keep-days", type=int, metavar="N",
                    help="then remove the backups dated more than N days before today (default: backup.keep_days)")
    ck = sub.add_parser("check", help="check the configuration (and the clients it names) without changing anything")
    ck.add_argument("--config-only", action="store_true", help="don't open the database")
    cl = sub.add_parser("client", help="register clients locally").add_subparsers(dest="action", required=True)
    add = cl.add_parser("add")
    add.add_argument("name")
    add.add_argument("role", choices=ALL_ROLES)
    add.add_argument("--label")
    add.add_argument("--token-file", required=True)
    add.add_argument("--deliverer", help="the deliverer that reaches this client")
    rot = cl.add_parser("rotate")
    rot.add_argument("name")
    rot.add_argument("--token-file", required=True)
    route = cl.add_parser("route", help="register a client with the deliverer that reaches it ('-': none)")
    route.add_argument("name")
    route.add_argument("deliverer")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        cfg = load(args.config)
    except (ConfigError, OSError, ValueError) as e:
        print(f"waypointd: configuration: {e}", file=sys.stderr)
        return 2
    if args.cmd in (None, "serve"):
        try:
            serve(cfg)
        except ConfigError as e:
            print(f"waypointd: configuration: {e}", file=sys.stderr)
            return 2
        except BindError as e:
            print(f"waypointd: {e}", file=sys.stderr)
            return 1
        except RuntimeError as e:  # e.g. a database with a newer schema than this version
            print(f"waypointd: {e}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "check":
        return dry_check(cfg, args.config_only)
    keep_days = getattr(args, "keep_days", None)
    if keep_days is not None and not 1 <= keep_days <= 3650:
        print("waypointd: --keep-days: a whole number from 1 to 3650", file=sys.stderr)
        return 2
    try:
        store = Store(connect(cfg.database, migrate_schema=False), settings=cfg.settings)
    except SchemaMismatch as e:
        print(f"waypointd: {e}", file=sys.stderr)
        return 1
    if args.cmd == "backup":
        from .monitor import backup, prune_backups
        try:
            db_path, json_path = backup(store, args.dir)
        except OSError as e:
            print(f"waypointd: backup: {e.strerror}", file=sys.stderr)
            return 1
        print(f"waypointd: wrote {db_path.name} and {json_path.name}")
        keep = keep_days if keep_days is not None else cfg.backup_keep_days
        if keep is None:
            return 0
        try:  # only after a backup that worked: a failing backup never costs an old one
            removed, failed = prune_backups(args.dir, store.now().date(), keep)
        except OSError as e:
            print(f"waypointd: backup: listing {args.dir} to remove old backups: {e.strerror}", file=sys.stderr)
            return 1
        if removed:
            print(f"waypointd: removed {len(removed)} backup files older than {keep} days: {', '.join(removed)}")
        for name, reason in failed:
            print(f"waypointd: backup: could not remove {name}: {reason}", file=sys.stderr)
        return 1 if failed else 0
    if args.action == "route":
        try:
            store.route(args.name, None if args.deliverer == "-" else args.deliverer)
        except ApiError as e:
            print(f"waypointd: {e.message}", file=sys.stderr)
            return 1
        print(f"waypointd: {args.name} is reached through {args.deliverer}", file=sys.stderr)
        return 0
    if args.action == "add":
        if args.deliverer:
            cl = store.client(args.deliverer)
            if cl is None or cl.role != "deliverer":
                print("waypointd: --deliverer: no such deliverer", file=sys.stderr)
                return 1

        def make():
            token = store.add_client(args.name, args.role, label=args.label)
            if args.deliverer:
                store.route(args.name, args.deliverer)
            return token
    else:
        make = lambda: store.rotate_token(args.name)  # noqa: E731
    try:
        issue_token(args.token_file, make)
    except FileExistsError:
        print("waypointd: the token file exists already; remove it or name another", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"waypointd: cannot write the token file: {e.strerror}", file=sys.stderr)
        return 1
    except ApiError as e:
        print(f"waypointd: {e.message}", file=sys.stderr)
        return 1
    # stderr: stdout stays empty, so a script that captures it (or pipes the token file after it)
    # gets nothing but what it asked for
    print(f"waypointd: token for {args.name} written to {args.token_file}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
