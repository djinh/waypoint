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


def load(path: str | Path | None) -> Config:
    cfg = Config()
    if path is None:
        return cfg
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    server = data.get("server", {})
    store = data.get("store", {})
    site = data.get("site", {})
    unknown = set(data) - {"server", "store", "site"}
    if unknown:
        raise ConfigError(f"unknown section: {sorted(unknown)[0]}")
    cfg.listen = list(server.get("listen", cfg.listen))
    cfg.port = int(server.get("port", cfg.port))
    cfg.database = str(store.get("database", cfg.database))
    s = cfg.settings
    s.tz = site.get("tz", s.tz)
    s.done_days = int(site.get("done_days", s.done_days))
    s.coordinator = site.get("coordinator", s.coordinator)
    for key in ("running_lease", "running_grace"):
        if key in site:
            try:
                setattr(s, f"{key}_seconds", int(parse_delay(site[key]).total_seconds()))
            except (ValueError, TypeError):
                raise ConfigError(f"site.{key}: a duration like 90m, 2h or 1d") from None
    if "default_ladder" in site:
        s.default_ladder = [{"after": x["after"], "action": x["action"], "to": x.get("to")} for x in site["default_ladder"]]
    check(cfg)
    return cfg


def check(cfg: Config) -> None:
    if not cfg.listen:
        raise ConfigError("server.listen: at least one address")
    for addr in cfg.listen:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise ConfigError(f"server.listen: {addr!r} is not an IP address") from None
        if ip.is_unspecified:
            raise ConfigError("server.listen: refusing to listen on every address; name each one")
    if not 0 < cfg.port < 65536:
        raise ConfigError("server.port: out of range")
    try:
        ZoneInfo(cfg.settings.tz)
    except Exception:
        raise ConfigError("site.tz: not an IANA time zone") from None
    for step in cfg.settings.default_ladder:
        try:
            schema.check("LadderStep", step)
        except Exception:
            raise ConfigError("site.default_ladder: not a valid ladder step") from None
