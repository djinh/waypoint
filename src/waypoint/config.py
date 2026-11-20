"""waypointd's configuration: a TOML file (see config.example.toml), with safe defaults."""

from __future__ import annotations

import ipaddress
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from . import schema
from .store import Settings
from .timeutil import parse_delay

DEFAULT_PORT = 7130


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    listen: list[str] = field(default_factory=lambda: ["127.0.0.1"])
    port: int = DEFAULT_PORT
    database: str = "/var/lib/waypoint/waypoint.db"
    settings: Settings = field(default_factory=Settings)


# Every key a section may hold; anything else is a typo that would otherwise load silently.
KEYS = {
    "server": {"listen", "port"},
    "store": {"database"},
    "site": {"tz", "coordinator", "running_lease", "running_grace", "done_days", "default_ladder"},
}


def _int(value, key: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ConfigError(f"{key}: a whole number from {lo} to {hi}")
    return value


def _ladder(value) -> list[dict]:
    """The default ladder from the raw TOML value, with the same rules as an item's own ladder."""
    if not isinstance(value, list):
        raise ConfigError("site.default_ladder: a list of steps like { after = \"1h\", action = \"escalate\" }")
    steps = []
    for i, raw in enumerate(value):
        key = f"site.default_ladder[{i}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{key}: a step like {{ after = \"1h\", action = \"escalate\" }}")
        try:
            schema.check("LadderStep", raw)
            parse_delay(raw["after"])
        except Exception:
            raise ConfigError(f"{key}: not a valid ladder step") from None
        if (raw["action"] == "notify") != ("to" in raw):
            raise ConfigError(f"{key}: \"to\" names the human to notify, for notify only")
        steps.append({"after": raw["after"], "action": raw["action"], "to": raw.get("to")})
    return steps


def load(path: str | Path | None) -> Config:
    cfg = Config()
    if path is None:
        return cfg
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    unknown = set(data) - set(KEYS)
    if unknown:
        raise ConfigError(f"unknown section: {sorted(unknown)[0]}")
    for section, allowed in KEYS.items():
        if not isinstance(data.get(section, {}), dict):
            raise ConfigError(f"{section}: a [{section}] table")
        extra = set(data.get(section, {})) - allowed
        if extra:
            raise ConfigError(f"unknown key: {section}.{sorted(extra)[0]}")
    server, store, site = (data.get(s, {}) for s in ("server", "store", "site"))
    if "listen" in server:
        listen = server["listen"]
        if not isinstance(listen, list) or not all(isinstance(a, str) for a in listen):
            raise ConfigError('server.listen: a list of addresses, like ["127.0.0.1"]')
        cfg.listen = list(listen)
    if "port" in server:
        cfg.port = _int(server["port"], "server.port", 1, 65535)
    if "database" in store:
        if not isinstance(store["database"], str) or not store["database"].strip():
            raise ConfigError("store.database: the path of the database file")
        cfg.database = store["database"]
    s = cfg.settings
    for key in ("tz", "coordinator"):
        if key in site:
            if not isinstance(site[key], str) or not site[key].strip():
                raise ConfigError(f"site.{key}: a name in quotes")
            setattr(s, key, site[key])
    if "done_days" in site:
        s.done_days = _int(site["done_days"], "site.done_days", 0, 3650)
    for key in ("running_lease", "running_grace"):
        if key in site:
            try:
                setattr(s, f"{key}_seconds", int(parse_delay(site[key]).total_seconds()))
            except (ValueError, TypeError):
                raise ConfigError(f"site.{key}: a duration like 90m, 2h or 1d") from None
    if "default_ladder" in site:
        s.default_ladder = _ladder(site["default_ladder"])
    check(cfg)
    return cfg


def check(cfg: Config) -> None:
    if not cfg.listen:
        raise ConfigError("server.listen: at least one address")
    seen = set()
    for addr in cfg.listen:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise ConfigError(f"server.listen: {addr!r} is not an IP address") from None
        if ip.is_unspecified:
            raise ConfigError("server.listen: refusing to listen on every address; name each one")
        if ip in seen:
            raise ConfigError(f"server.listen: {addr!r} is listed twice")
        seen.add(ip)
    if not 0 < cfg.port < 65536:
        raise ConfigError("server.port: out of range")
    try:
        ZoneInfo(cfg.settings.tz)
    except Exception:
        raise ConfigError("site.tz: not an IANA time zone") from None
    for step in cfg.settings.default_ladder:
        try:
            schema.check("LadderStep", {k: v for k, v in step.items() if v is not None})
        except Exception:
            raise ConfigError("site.default_ladder: not a valid ladder step") from None


def check_clients(settings: Settings, clients: dict[str, str]) -> None:
    """The clients the configuration names exist with the right role. CLIENTS maps each live
    (unretired) client's name to its role. A configured coordinator must be a coordinator, or
    escalations go nowhere; a notify step of the default ladder must name a human."""
    name = settings.coordinator
    if name is not None and clients.get(name) != "coordinator":
        raise ConfigError(f"site.coordinator: {name!r} is not a registered coordinator")
    for i, step in enumerate(settings.default_ladder):
        to = step.get("to")
        if to is not None and clients.get(to) != "human":
            raise ConfigError(f"site.default_ladder[{i}].to: {to!r} is not a registered human")
