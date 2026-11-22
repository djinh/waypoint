"""The deliverer's adapters: how a message reaches a recipient.

An adapter's ``deliver(delivery)`` returns DELIVERED, RETRY (try again later; the deliverer gives
up after 30 minutes), BUSY (the recipient is working; try again later, give up only after hours) or
an Undeliverable with a short reason. Reasons never quote pane contents or
URLs. Pane addresses, push URLs and webhook URLs come only from the deliverer's configuration.
"""

from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("waypoint.deliver.tmux")

DELIVERED = "delivered"
RETRY = "retry"
BUSY = "busy"
# the adapter cleared its own line that wasn't taken: try again, but not for a while (the deliverer
# waits at least a minute, so a pane where Enter keeps failing isn't retyped every few seconds)
CLEARED = "cleared"


@dataclass
class Undeliverable:
    reason: str


# ---------------------------------------------------------------------- tmux

_ANSI_DIM = re.compile(r"\x1b\[2m.*?\x1b\[(?:0|22)m", re.S)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# A horizontal rule of the input box: dashes only, or (a session started with a name) dashes with a
# short label near the end, "──────── Session Name ─". A line that starts with dashes and goes on
# with text is not a rule.
_RULE = re.compile(r"^\s*─{8,}(?: (?!─)[^\n]{1,60}? ─+)?\s*$")
_PROMPT = re.compile(r"^\s*[❯>]")
# Claude Code's status line above the input box while a turn runs: a glyph, a verb with "…", then the
# elapsed time in brackets ("✢ Thinking… (51s · ↓ 3.2k tokens)"). Idle it reads "✻ Worked for 14s".
# Older versions said "esc to interrupt" there.
_BUSY = re.compile(r"^\s*\S\s+\S+…\s*\((?:\d+h\s*)?(?:\d+m\s*)?\d+s\b|esc to interrupt")
# A line submitted while a turn runs leaves the box and waits above it, marked "⎿  Queued · …"
_QUEUED = re.compile(r"^\s*⎿\s+Queued\b")
# C0 and C1 control characters and DEL; a message must never act as keys
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def input_box(capture: str) -> list[str] | None:
    """The lines of the input box of a Claude Code pane, colours and dim suggestions removed.

    The box is what lies between the last two horizontal rules of the screen. None when there is
    no such box (a menu, a dialog, a pane that isn't Claude Code): then nothing may be typed."""
    lines = [_ANSI.sub("", _ANSI_DIM.sub("", line)) for line in capture.splitlines()]
    rules = [i for i, line in enumerate(lines) if _RULE.match(line)]
    if len(rules) < 2:
        return None
    top, bottom = rules[-2], rules[-1]
    box = lines[top + 1:bottom]
    if not box or not _PROMPT.match(box[0]):
        return None
    return box


def pane_busy(capture: str) -> bool:
    """True if the lines just above the input box show a turn running. A line typed then can be taken
    as a paste and lose its Enter, so the adapter waits until the session is idle."""
    lines = [_ANSI.sub("", line) for line in capture.splitlines()]
    rules = [i for i, line in enumerate(lines) if _RULE.match(line)]
    if len(rules) < 2:
        return False
    above = [line for line in lines[:rules[-2]] if line.strip()][-4:]
    return any(_BUSY.search(line) for line in above)


def queued(capture: str, line: str) -> bool:
    """True if `line` waits above the input box as a queued message: the lines just before a
    "⎿  Queued" mark, from the prompt sign on, are that line (compared without whitespace)."""
    lines = [_ANSI.sub("", x) for x in capture.splitlines()]
    rules = [i for i, x in enumerate(lines) if _RULE.match(x)]
    if len(rules) < 2:
        return False
    above = lines[:rules[-2]]
    ours = _squash(line)
    for i, mark in enumerate(above):
        if not _QUEUED.match(mark):
            continue
        for j in range(i - 1, max(i - 40, -1), -1):  # a long line wraps over several screen lines
            if _PROMPT.match(above[j]):
                if _squash(_PROMPT.sub("", " ".join(above[j:i]), count=1)) == ours:
                    return True
                break
    return False


def box_text(capture: str) -> str | None:
    """What the input box holds, as one line (wrapped lines joined), or None without a box."""
    box = input_box(capture)
    if box is None:
        return None
    text = _PROMPT.sub("", " ".join(box), count=1).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def input_is_empty(capture: str) -> bool:
    """True only if the input box exists and holds nothing but the prompt (and suggestions)."""
    box = input_box(capture)
    if box is None:
        return False
    text = "".join(box)
    text = _PROMPT.sub("", text, count=1)
    return text.replace(" ", " ").strip() == ""


def one_line(text: str, limit: int = 2000) -> str:
    """The message as literal text on one line: control characters and newlines become spaces."""
    flat = _CONTROL.sub(" ", text)
    return re.sub(r" {2,}", " ", flat).strip()[:limit]


# "ref" messages: what is typed comes only from these fixed words and a checked item id, never from
# text a client wrote (a title, label, note or owner). A typed line arrives as the session's own user
# input, so client-written text there would be a prompt-injection path.
MESSAGE_MODES = ("full", "ref")
_REF_KIND = {
    "wake": "reminder",
    "unblocked": "can start",
    "replan": "re-plan",
    "at_risk": "at risk",
    "nudge": "late",
    "escalate": "escalation",
    "notify": "late",
    "undeliverable": "undeliverable",
    "deliverer_silent": "deliverer silent",
    "outage": "outage summary",
    "running_check": "still running?",
    "recurring": "recurring",
}
_REF_NO_ITEM = {
    "outage": "Waypoint was down for a while (outage summary): read it with wp_messages",
    "deliverer_silent": "a deliverer stopped fetching its messages (deliverer silent): read it with wp_messages",
    "undeliverable": "a message could not be delivered (undeliverable): read it with wp_messages",
    "recurring": "a recurrence needs attention (recurring): read it with wp_messages",
}
_REF_FALLBACK = "check your items with wp_mine"
_ITEM_ID = re.compile(r"W-[1-9][0-9]{0,9}")


def ref_line(delivery: dict) -> str:
    """The fixed sentence a "ref" recipient gets: the item id and a fixed word for the kind, or a
    fixed sentence; anything unexpected gives the fallback. No client-written text, ever."""
    kind = delivery.get("kind")
    item = delivery.get("item")
    word = _REF_KIND.get(kind) if isinstance(kind, str) else None
    if word is None:
        return _REF_FALLBACK
    if item is None:
        return _REF_NO_ITEM.get(kind, _REF_FALLBACK)
    if not isinstance(item, str) or not _ITEM_ID.fullmatch(item):
        return _REF_FALLBACK
    return f"wake-up for {item} ({word}): read it with wp_get {item}"


class TmuxAdapter:
    """Types the message into a tmux pane, but only into an empty input box.

    message="full" types the message's text; message="ref" types only a fixed sentence with the
    item id (see ref_line), for sessions that must never receive text written by other clients."""

    DEFAULT_SETTLE = 1.0
    # how long to watch a pane after Enter before calling the line not taken: a loaded machine can
    # redraw a pane seconds after the session took the line
    DEFAULT_WATCH = 8.0

    def __init__(self, target: str, socket: str | None = None, prefix: str = "[waypoint] ",
                 tmux: str = "tmux", run=None, timeout: float = 5, message: str = "full",
                 settle: float | None = None, watch: float | None = None, sleep=time.sleep):
        if message not in MESSAGE_MODES:
            raise ValueError("message must be full or ref")
        # seconds between looks at the pane after Enter, and how long to keep looking
        self.settle = self.DEFAULT_SETTLE if settle is None else settle
        self.watch = self.DEFAULT_WATCH if watch is None else watch
        self._sleep = sleep
        self.target = target
        self.socket = socket
        self.prefix = prefix
        self.message = message
        self.tmux = tmux
        self.timeout = timeout
        self._run = run or self._subprocess
        # per message id: how often its line wasn't taken (logged the first time, summed up at the end)
        self._not_taken: dict[str, int] = {}

    def _subprocess(self, args: list[str]) -> tuple[int, str]:
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return 127, ""
        return p.returncode, p.stdout

    def _tmux(self, *args: str) -> tuple[int, str]:
        base = [self.tmux]
        if self.socket:
            # a path is a socket file (-S); anything else a socket name (-L)
            base += ["-S", self.socket] if self.socket.startswith("/") else ["-L", self.socket]
        return self._run(base + list(args))

    def capture(self) -> str | None:
        code, out = self._tmux("capture-pane", "-p", "-e", "-t", self.target)
        return out if code == 0 else None

    def ready(self):
        """True if the message may be typed now; otherwise RETRY or BUSY.

        Not ready: no pane, a mode (in copy mode capture-pane still shows the normal screen, but
        typed keys go to the mode), anything in the input box or no box (RETRY), or a turn running
        (BUSY: a line typed then can lose its Enter, and the session would only read it at the
        end of the turn anyway). Whatever the box holds is never touched, also a line an earlier
        attempt typed: only the attempt that typed a line may clear it."""
        if self._in_mode():
            return RETRY
        cap = self.capture()
        if cap is None or input_box(cap) is None:
            return RETRY
        if pane_busy(cap):
            return BUSY  # whatever the box holds: it may be text typed during the turn
        return True if input_is_empty(cap) else RETRY

    def _in_mode(self) -> bool:
        """True if the pane is in a mode (copy mode, a choose list), or tmux can't say. capture-pane
        still shows the normal screen then, but keys go to the mode: an Enter there would end a
        person's copy mode and copy their selection."""
        code, out = self._tmux("display-message", "-p", "-t", self.target, "#{pane_in_mode}")
        return code != 0 or out.strip() != "0"

    def settled(self, delivery_id: str) -> None:
        """The deliverer is done with this message: log the last of its not-taken cases, if repeated."""
        count = self._not_taken.pop(delivery_id, 0)
        if count > 1:
            log.warning("pane %s: %s's line wasn't taken %d times in all", self.target, delivery_id, count)

    def deliver(self, delivery: dict):
        state = self.ready()
        if state is not True:
            return state
        body = ref_line(delivery) if self.message == "ref" else delivery["text"]
        text = one_line(self.prefix + body)
        if text.endswith(";"):
            text += " "  # tmux reads a trailing ";" as a command separator, even with -l
        # look again right before typing: never type over someone who just started
        state = self.ready()
        if state is not True:
            return state
        code, _ = self._tmux("send-keys", "-t", self.target, "-l", "--", text)
        if code != 0:
            return RETRY
        return self._submit(text, delivery.get("id", ""))

    def _watch(self, ours: str) -> str:
        """After an Enter: look at the pane every `settle` seconds for up to `watch` seconds.

        "taken" as soon as the box no longer starts with our line, our line waits above the box as
        a queued message, or the pane is busy (it was idle when we typed, so the turn is ours);
        "added" when the box holds our line with someone's text after it; "stuck" when the box
        still holds exactly our line, in an idle pane, at the end."""
        looks = max(1, math.ceil(self.watch / self.settle)) if self.settle > 0 else max(1, int(self.watch))
        for look in range(looks):
            self._sleep(self.settle)
            cap = self.capture()
            if cap is None:
                return "taken"  # the pane is gone or changed beyond a box: nothing of ours is left there
            now = _squash(box_text(cap) or "")
            if not now.startswith(ours) or queued(cap, ours) or pane_busy(cap):
                return "taken"
            if now != ours:
                return "added"
        return "stuck"

    def _submit(self, text: str, delivery_id: str = ""):
        """Enter, then make sure it took (see _watch). Only if the box still holds exactly our line in
        an idle pane after the whole watch: Enter once more and watch again. If it is still stuck,
        clear it (this attempt typed it, and nothing was added) and try again in a minute; if our
        line got text added, report "typed, but not submitted" and never press Enter on it.
        Before each of those keys the pane must not be in a mode (the watch can't see one): if it is,
        nothing more is sent. Lines are compared without whitespace, because Claude Code wraps long input."""
        ours = _squash(text)
        name = delivery_id or "a message"
        first = delivery_id not in self._not_taken
        for attempt in range(2):
            code, _ = self._tmux("send-keys", "-t", self.target, "Enter")
            if code != 0:
                return Undeliverable("typed, but Enter failed")
            seen = self._watch(ours)
            if seen == "taken":
                return DELIVERED
            if seen == "added":
                return Undeliverable("typed, but not submitted")  # never press Enter on someone's text
            if self._in_mode():
                # someone is scrolling back (or tmux can't say): never a key into a mode; our line stays
                log.warning("pane %s: %s's line wasn't taken, and the pane is in a mode; left alone",
                            self.target, name)
                return Undeliverable("typed, but not submitted")
            if attempt == 0 and first:
                log.warning("pane %s: %s's line wasn't taken; pressing Enter once more", self.target, name)
        # still exactly our line, typed by this attempt, idle for two whole watches: clear it
        why = f"{name}'s line after two Enters; trying again in a minute"
        code, _ = self._tmux("send-keys", "-t", self.target, "C-u")
        cap = self.capture() if code == 0 else None
        if cap is None or not input_is_empty(cap):
            log.warning("pane %s: tried to clear %s, but the box isn't empty; left alone", self.target, why)
            return Undeliverable("typed, but not submitted")
        if first:
            log.warning("pane %s: cleared %s", self.target, why)
        self._not_taken[delivery_id] = self._not_taken.get(delivery_id, 0) + 1
        return CLEARED  # nothing left behind; typed again, but not for a while


# ---------------------------------------------------------------------- HTTP adapters

def _post(url: str, data: bytes, headers: dict, timeout: float):
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(65536)  # the answer's body doesn't matter; never read an endless one
            return DELIVERED
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500 and e.code not in (408, 429):
            return Undeliverable(f"the endpoint refused it (HTTP {e.code})")
        return RETRY
    except (urllib.error.URLError, OSError):
        return RETRY


class PushAdapter:
    """A push notification, e.g. to an ntfy topic: the summary (title and owner) only, because push
    channels may be public."""

    def __init__(self, url: str, title: str = "Waypoint", priority: str | None = None, timeout: float = 10):
        self.url = url
        self.title = title
        self.priority = priority
        self.timeout = timeout

    def deliver(self, delivery: dict):
        headers = {"Title": one_line(self.title, 100), "Content-Type": "text/plain; charset=utf-8"}
        if self.priority:
            headers["Priority"] = one_line(self.priority, 20)
        return _post(self.url, one_line(delivery["summary"], 140).encode("utf-8"), headers, self.timeout)


class WebhookAdapter:
    """A POST of the delivery as JSON, for other people's setups."""

    def __init__(self, url: str, timeout: float = 10):
        self.url = url
        self.timeout = timeout

    def deliver(self, delivery: dict):
        body = json.dumps({"delivery": delivery}).encode("utf-8")
        return _post(self.url, body, {"Content-Type": "application/json"}, self.timeout)
