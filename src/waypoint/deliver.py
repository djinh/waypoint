"""waypoint-deliver: fetches this host's messages from Waypoint and hands each to its adapter.

    waypoint-deliver --config deliver.toml

One deliverer runs per host and user where recipients live. It long-polls Waypoint, keeps the
messages it is working on (and ignores them when they are handed out again), retries the ones
whose adapter can't deliver yet, and gives up after 30 minutes from the first hand-out with an
`undeliverable` confirmation, which Waypoint escalates. When Waypoint is down it backs off and
tries again; a host that slept simply picks up its queue when it wakes.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import tomllib
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import (BUSY, CLEARED, DELIVERED, MESSAGE_MODES, RETRY, PushAdapter, TmuxAdapter, Undeliverable,
                       WebhookAdapter)
from .client import ApiFailure, Unavailable, WaypointClient, read_token

log = logging.getLogger("waypoint.deliver")

GIVE_UP_SECONDS = 30 * 60
# A recipient that is busy (a turn running) gets its message when the turn ends; give up only if it
# stays busy for this long (a session that never stops is worth telling the coordinator about).
BUSY_GIVE_UP_SECONDS = 4 * 3600
RETRY_SECONDS = 5
CLEARED_SECONDS = 60  # after the adapter cleared a line that wasn't taken
REMEMBER = 2000  # delivered ids kept, so a re-hand-out after a lost confirmation isn't delivered twice


class ConfigError(ValueError):
    pass


@dataclass
class Pending:
    delivery: dict
    first_seen: float  # wall-clock seconds of the first hand-out we know of
    next_try: float = 0.0
    # When the recipient last became blocked (idle, but nothing can be typed); None while it is busy.
    # Before it was ever busy, blocking counts from the first hand-out.
    blocked_since: float | None = None
    ever_busy: bool = False

    def deadline(self) -> float:
        """When to give up: 4 hours after the first hand-out at the latest, and 30 minutes after the
        recipient became blocked, if it is blocked now."""
        overall = self.first_seen + BUSY_GIVE_UP_SECONDS
        blocked = self.blocked_since if self.blocked_since is not None else (
            None if self.ever_busy else self.first_seen)
        return overall if blocked is None else min(overall, blocked + GIVE_UP_SECONDS)


@dataclass
class Deliverer:
    client: WaypointClient
    adapters: dict  # recipient name -> adapter
    clock: object = time.time
    lease_seconds: int = 120
    pending: dict[str, Pending] = field(default_factory=dict)
    done: "OrderedDict[str, str]" = field(default_factory=OrderedDict)  # id -> result not yet confirmed or confirmed
    unconfirmed: dict[str, dict] = field(default_factory=dict)  # id -> ack body still to send

    # ------------------------------------------------------------------ one round
    def poll(self, wait: int) -> None:
        """Fetch (waiting up to `wait` seconds) and take in what came."""
        got = self.client.request("GET", "/v1/deliveries", query={"wait": wait}, timeout=wait + 15)
        self.lease_seconds = got.get("lease_seconds", self.lease_seconds)
        now = self.clock()
        for d in got["deliveries"]:
            did = d["id"]
            if did in self.done:
                # delivered already; only the confirmation got lost
                self.unconfirmed.setdefault(did, {"result": self.done[did]})
                continue
            if did in self.pending:
                self.pending[did].delivery = d
                continue
            # a message handed out before (attempt > 1) was first handed out about this long ago
            first = now - (d.get("attempt", 1) - 1) * self.lease_seconds
            self.pending[did] = Pending(d, first_seen=first)

    def work(self) -> None:
        """Try every pending message that is due for a try; confirm what is settled."""
        now = self.clock()
        for did, p in list(self.pending.items()):
            if p.next_try > now and now < p.deadline():
                continue
            d = p.delivery
            adapter = self.adapters.get(d["recipient"])
            if adapter is None:
                result = Undeliverable("no adapter configured for this recipient")
            else:
                try:
                    result = adapter.deliver(d)
                except Exception as exc:  # an adapter bug must not stop the deliverer
                    log.error("adapter failed on %s: %s", did, type(exc).__name__)
                    result = RETRY
            if result in (BUSY, RETRY, CLEARED):
                if result == BUSY:
                    p.ever_busy, p.blocked_since = True, None
                elif p.blocked_since is None:
                    p.blocked_since = now if p.ever_busy else p.first_seen
                if now >= p.deadline():
                    blocked_long = p.blocked_since is not None and now >= p.blocked_since + GIVE_UP_SECONDS
                    result = Undeliverable("no chance to deliver within 30 minutes" if blocked_long
                                           else "the recipient stayed busy for 4 hours")
                else:
                    p.next_try = now + (CLEARED_SECONDS if result == CLEARED else RETRY_SECONDS)
                    continue
            body = {"result": "delivered"} if result == DELIVERED else {"result": "undeliverable",
                                                                         "detail": result.reason}
            del self.pending[did]
            if hasattr(adapter, "settled"):
                adapter.settled(did)  # the adapter's summary of repeated cases for this message
            self._remember(did, body["result"])
            self.unconfirmed[did] = body
            log.info("%s for %s (%s): %s", did, d["recipient"], d["kind"], body["result"])
        self.confirm()

    def confirm(self) -> None:
        for did, body in list(self.unconfirmed.items()):
            try:
                self.client.request("POST", f"/v1/deliveries/{did}/ack", body)
            except ApiFailure as e:
                if e.code != "not_found":
                    log.warning("confirming %s refused: %s", did, e.code)
            except Unavailable:
                return  # try the rest later
            del self.unconfirmed[did]

    def _remember(self, did: str, result: str) -> None:
        self.done[did] = result
        while len(self.done) > REMEMBER:
            self.done.popitem(last=False)

    # ------------------------------------------------------------------ the loop
    def run(self, stop=lambda: False, sleep=time.sleep) -> None:
        backoff = 1
        while not stop():
            try:
                self.poll(wait=RETRY_SECONDS if (self.pending or self.unconfirmed) else 60)
                backoff = 1
            except Unavailable:
                log.warning("Waypoint unavailable; trying again in %s s", backoff)
                self.work()  # messages already here can still go out
                sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            except ApiFailure as e:
                log.error("Waypoint refused the fetch: %s", e.code)
                sleep(60)
                continue
            self.work()


# ---------------------------------------------------------------------- configuration

def build_adapters(recipients: dict, prefix: str, message: str = "full") -> dict:
    if message not in MESSAGE_MODES:
        raise ConfigError('waypoint.message must be "full" or "ref"')
    out = {}
    for name, rc in recipients.items():
        kind = rc.get("adapter")
        if "message" in rc and kind != "tmux":
            raise ConfigError(f"recipient {name}: message is a tmux option")
        if rc.get("message", message) not in MESSAGE_MODES:
            raise ConfigError(f'recipient {name}: message must be "full" or "ref"')
        try:
            if kind == "tmux":
                out[name] = TmuxAdapter(rc["target"], socket=rc.get("socket"), prefix=rc.get("prefix", prefix),
                                        message=rc.get("message", message))
            elif kind == "push":
                out[name] = PushAdapter(rc["url"], title=rc.get("title", "Waypoint"), priority=rc.get("priority"))
            elif kind == "webhook":
                out[name] = WebhookAdapter(rc["url"])
            else:
                raise ConfigError(f"recipient {name}: adapter must be tmux, push or webhook")
        except KeyError as e:
            raise ConfigError(f"recipient {name}: missing {e.args[0]}") from None
        if kind in ("push", "webhook") and not str(rc["url"]).startswith(("https://", "http://")):
            raise ConfigError(f"recipient {name}: url must be http(s)")
    return out


def load(path: str) -> tuple[WaypointClient, dict]:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    wp = data.get("waypoint", {})
    if "token_file" not in wp:
        raise ConfigError("waypoint.token_file is required")
    url = wp.get("url", "http://127.0.0.1:7130")
    client = WaypointClient(url, read_token(wp["token_file"]), timeout=30)
    adapters = build_adapters(data.get("recipient", {}), wp.get("prefix", "[waypoint] "),
                              wp.get("message", "full"))
    return client, adapters


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="waypoint-deliver", description="Deliver Waypoint's messages on this host")
    p.add_argument("--config", required=True)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        client, adapters = load(args.config)
    except (ConfigError, OSError, ValueError, tomllib.TOMLDecodeError) as e:
        print(f"waypoint-deliver: configuration: {e}", file=sys.stderr)
        return 2
    log.info("delivering for %d recipient(s)", len(adapters))
    Deliverer(client, adapters).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
