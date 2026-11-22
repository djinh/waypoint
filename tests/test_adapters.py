"""The deliverer's adapters: tmux (empty input box only), push (summary only), webhook."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from waypoint.adapters import (_ANSI, _RULE, DELIVERED, RETRY, PushAdapter, TmuxAdapter, Undeliverable, WebhookAdapter,
                               input_box, input_is_empty, one_line)

GREY = "\x1b[38;5;245m"
DIMGREY = "\x1b[38;5;241m"
RESET = "\x1b[39m"
RULE = GREY + "─" * 100 + RESET


def pane(*box: str, above: str = "Tip: run the tests before you push") -> str:
    """A made-up screen in the shape of a Claude Code pane: output, a tip, the input box between two
    rules, and the footer."""
    return "\n".join([
        "● Done. The tests pass.",
        "",
        f"{DIMGREY}  ⎿  {above}{RESET}",
        RULE,
        *box,
        RULE,
        f"{RESET}  {DIMGREY}Model · ctx 12%{RESET}",
        "  \x1b[38;5;131m⏵⏵ accept edits on\x1b[38;5;241m (shift+tab to cycle)\x1b[39m",
    ])


EMPTY = pane(f"{DIMGREY}❯ {RESET}")
SUGGESTION = pane("\x1b[39m❯ \x1b[2mrun the migration tests next\x1b[0m")
SUGGESTION_22 = pane("❯ \x1b[2mcommit and push\x1b[22m")
TYPED = pane("\x1b[39m❯ please look at the failing test")
TYPED_THEN_SUGGESTION = pane("❯ fix\x1b[2m the failing test\x1b[0m")
TYPED_TWO_LINES = pane("❯ first line", "  second line")
MENU = pane("❯ 1. Yes", "  2. No, and tell Claude what to do differently")
NO_BOX = "Select login method:\n❯ 1. Account\n  2. API key\n"
ONE_RULE = "output\n" + RULE + "\n❯ \n"


@pytest.mark.parametrize("screen", [EMPTY, SUGGESTION, SUGGESTION_22])
def test_empty_input_box(screen):
    assert input_is_empty(screen)


@pytest.mark.parametrize("screen", [TYPED, TYPED_THEN_SUGGESTION, TYPED_TWO_LINES, MENU, NO_BOX, ONE_RULE, ""])
def test_not_empty_or_no_box(screen):
    assert not input_is_empty(screen)


def test_one_line_removes_control_characters():
    assert one_line("a\nb\r\nc\td\x1b[31m red\x03 \x7f\x9b x") == "a b c d [31m red x"
    assert one_line("x" * 3000) == "x" * 2000


def with_box(screen: str, text: str) -> str:
    """The screen with `text` in its input box (what Claude Code shows once it has read typed text)."""
    lines = screen.splitlines()
    rules = [i for i, line in enumerate(lines) if _RULE.match(_ANSI.sub("", line))]
    top, bottom = rules[-2], rules[-1]
    return "\n".join(lines[:top + 1] + [f"❯\u00a0{text}"] + lines[bottom:])


class FakeTmux:
    """Records tmux calls and answers capture-pane from a list of screens, and pane_in_mode from
    a list of modes.

    Between typing and Enter, the box of the current screen shows the typed text (if `echo` and the
    box is empty), and the list of screens isn't advanced: the screens are what the pane shows before
    typing and after each Enter."""

    def __init__(self, screens, fail=(), modes=("0",), echo=True, shows=None, late=0):
        self.screens = list(screens)
        self.modes = list(modes)
        self.calls = []
        self.fail = set(fail)
        self.echo = echo
        self.shows = shows  # what the box shows for the typed text, if not the text itself
        self.late = late  # looks before the typed text shows
        self.typed = None  # typed, no Enter yet
        self.last = None

    def __call__(self, args):
        self.calls.append(args)
        verb = args[3] if args[1] in ("-L", "-S") else args[1]
        if verb in self.fail:
            return 1, ""
        if verb == "capture-pane":
            if self.typed is not None:
                screen = self.last or self.screens[0]
                if self.late:
                    self.late -= 1
                    return 0, screen
                shown = self.typed if self.shows is None else self.shows
                return 0, with_box(screen, shown) if self.echo and input_is_empty(screen) else screen
            self.last = self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
            return 0, self.last
        if verb == "display-message":
            return 0, (self.modes.pop(0) if len(self.modes) > 1 else self.modes[0]) + "\n"
        if verb == "send-keys":
            self.typed = args[-1] if "-l" in args else None
        return 0, ""

    def sent(self):
        return [a for a in self.calls if "send-keys" in a]


DELIVERY = {"id": "D-7", "recipient": "agent-a", "kind": "wake", "text": "Reminder: W-1 (soak)\nnext line",
            "summary": "Check the soak (agent-a)"}


def test_tmux_types_into_an_empty_box():
    fake = FakeTmux([SUGGESTION])
    adapter = TmuxAdapter("main", socket="agents-test", run=fake)
    assert adapter.deliver(DELIVERY) == DELIVERED
    assert fake.calls[0] == ["tmux", "-L", "agents-test", "display-message", "-p", "-t", "main", "#{pane_in_mode}"]
    assert fake.calls[1] == ["tmux", "-L", "agents-test", "capture-pane", "-p", "-e", "-t", "main"]
    assert fake.sent() == [
        ["tmux", "-L", "agents-test", "send-keys", "-t", "main", "-l", "--", "[waypoint] Reminder: W-1 (soak) next line"],
        ["tmux", "-L", "agents-test", "send-keys", "-t", "main", "Enter"],
    ]


@pytest.mark.parametrize("screens", [[TYPED], [MENU], [NO_BOX], [EMPTY, TYPED]])
def test_tmux_never_types_over_someone(screens):
    fake = FakeTmux(screens)
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == RETRY
    assert fake.sent() == []


@pytest.mark.parametrize("modes", [["1"], ["0", "1"], [""]])
def test_tmux_never_types_into_a_mode(modes):
    """Copy mode (or any mode), also when it starts between the two looks."""
    fake = FakeTmux([EMPTY], modes=modes)
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == RETRY
    assert fake.sent() == []


def test_tmux_trailing_semicolon_is_kept():
    fake = FakeTmux([EMPTY])
    TmuxAdapter("main", run=fake).deliver(dict(DELIVERY, text="run it;"))
    assert fake.sent()[0][-1] == "[waypoint] run it; "


def test_tmux_socket_path_uses_s():
    fake = FakeTmux([EMPTY])
    TmuxAdapter("main", socket="/run/agents/tmux.sock", run=fake).deliver(DELIVERY)
    assert fake.calls[0][:3] == ["tmux", "-S", "/run/agents/tmux.sock"]


def test_tmux_missing_pane_or_server_is_retried():
    fake = FakeTmux([EMPTY], fail={"capture-pane"})
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == RETRY
    assert fake.sent() == []
    missing = TmuxAdapter("main", tmux="/nonexistent/tmux")
    assert missing.deliver(DELIVERY) == RETRY


def test_tmux_enter_failing_is_reported():
    fake = FakeTmux([EMPTY])
    real = fake.__call__

    def run(args):
        if args[-1] == "Enter":
            return 1, ""
        return real(args)

    result = TmuxAdapter("main", run=run).deliver(DELIVERY)
    assert isinstance(result, Undeliverable) and result.reason == "typed, but Enter failed"


# A pane that behaves like Claude Code's input box: it reads keys raw, redraws typed text inside the
# box (after a short delay, as a busy session would), and on Enter moves the line above the box.
FAKE_PANE = r"""
import os, sys, termios, time, tty
tty.setraw(0)
out, line = [], ""
def draw():
    rule = "\u2500" * 60
    box = line or "\x1b[2mtry the tests\x1b[0m"
    sys.stdout.write("\x1b[H\x1b[2J" + "".join(o + "\r\n" for o in ["output"] + out[-8:])
                     + rule + "\r\n\u276f\u00a0" + box + "\r\n" + rule + "\r\n")
    sys.stdout.flush()
draw()
while True:
    data = os.read(0, 4096).decode("utf-8", "replace")
    if not data:
        break
    time.sleep(0.2)
    for ch in data:
        if ch == "\r":
            out.append("\u276f " + line)
            line = ""
        elif ch == "\x15":
            line = ""
        elif ch >= " ":
            line += ch
    draw()
"""


@pytest.fixture
def real_pane(tmp_path):
    """A real tmux server on a socket file in tmp_path, with a pane that acts like Claude Code's
    input box (FAKE_PANE)."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    sock = str(tmp_path / "tmux.sock")
    script = tmp_path / "fake_pane.py"
    script.write_text(FAKE_PANE, encoding="utf-8")
    subprocess.run(["tmux", "-S", sock, "-f", "/dev/null", "new-session", "-d", "-s", "t", "-x", "120", "-y", "20",
                    f"{shlex.quote(sys.executable)} -I {shlex.quote(str(script))}"], check=True, timeout=10)
    adapter = TmuxAdapter("t", socket=sock, settle=0.3, watch=3)
    for _ in range(50):
        if "try the tests" in (adapter.capture() or ""):
            break
        time.sleep(0.1)
    yield adapter, sock
    subprocess.run(["tmux", "-S", sock, "kill-server"], timeout=10, capture_output=True)


def wait_for(adapter, text):
    for _ in range(50):
        cap = adapter.capture() or ""
        if text in cap:
            return cap
        time.sleep(0.1)
    return cap


def test_real_tmux_types_literally(real_pane):
    adapter, sock = real_pane
    assert input_is_empty(adapter.capture())
    msg = dict(DELIVERY, text="C-c ; kill-server $(echo no) `echo no` and;")
    assert adapter.deliver(msg) == DELIVERED
    cap = wait_for(adapter, "and;")
    assert "[waypoint] C-c ; kill-server $(echo no) `echo no` and;" in cap  # typed literally, ";" kept
    assert subprocess.run(["tmux", "-S", sock, "has-session", "-t", "t"], timeout=5).returncode == 0


def test_real_tmux_copy_mode_is_not_typed_into(real_pane):
    adapter, sock = real_pane
    subprocess.run(["tmux", "-S", sock, "copy-mode", "-t", "t"], check=True, timeout=5)
    assert input_is_empty(adapter.capture())  # the screen looks ready...
    assert adapter.deliver(DELIVERY) == RETRY  # ...but the keys would go to copy mode
    subprocess.run(["tmux", "-S", sock, "send-keys", "-t", "t", "-X", "cancel"], check=True, timeout=5)
    assert adapter.deliver(DELIVERY) == DELIVERED
    assert "[waypoint] Reminder: W-1 (soak) next line" in wait_for(adapter, "next line")


# ---------------------------------------------------------------------- HTTP adapters


class Target:
    """An HTTP endpoint on 127.0.0.1 that records requests and answers with a set status."""

    def __init__(self):
        self.requests = []
        self.status = 200
        target = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                target.requests.append((dict(self.headers), body))
                self.send_response(target.status)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def target():
    t = Target()
    yield t
    t.close()


def test_push_sends_the_summary_only(target):
    adapter = PushAdapter(target.url, title="Waypoint", priority="high")
    assert adapter.deliver(DELIVERY) == DELIVERED
    headers, body = target.requests[0]
    assert body.decode() == "Check the soak (agent-a)"
    assert headers["Title"] == "Waypoint" and headers["Priority"] == "high"
    assert b"Reminder" not in body


def test_webhook_sends_the_delivery(target):
    assert WebhookAdapter(target.url).deliver(DELIVERY) == DELIVERED
    headers, body = target.requests[0]
    assert json.loads(body) == {"delivery": DELIVERY}
    assert headers["Content-Type"] == "application/json"


@pytest.mark.parametrize("status,result", [(500, RETRY), (503, RETRY), (429, RETRY), (404, "undeliverable")])
def test_http_failures(target, status, result):
    target.status = status
    got = WebhookAdapter(target.url).deliver(DELIVERY)
    if result == "undeliverable":
        assert isinstance(got, Undeliverable) and got.reason == "the endpoint refused it (HTTP 404)"
        assert target.url not in got.reason
    else:
        assert got == result


def test_unreachable_endpoint_is_retried():
    t = Target()
    url = t.url
    t.close()
    assert PushAdapter(url, timeout=2).deliver(DELIVERY) == RETRY


# ---------------------------------------------------------------------- a session started with a name

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def named(prompt_line: str | None = None, busy: bool = True) -> str:
    """A real capture of a named, busy session (anonymised: only the box and what is around it), with
    its prompt line replaced if given, and the spinner line dropped for an idle one."""
    lines = (FIXTURES / "named-session-busy.cap").read_text(encoding="utf-8").splitlines()
    if prompt_line is not None:
        lines = [prompt_line if ANSI_FREE(line).startswith("❯") else line for line in lines]
    if not busy:
        lines = [line for line in lines if "Slithering" not in line]
    return "\n".join(lines) + "\n"


def ANSI_FREE(line: str) -> str:
    import re
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line)


def test_the_real_capture_has_a_label_in_its_top_rule():
    raw = (FIXTURES / "named-session-busy.cap").read_text(encoding="utf-8")
    assert "─ Session Name ─" in ANSI_FREE(raw)


@pytest.mark.parametrize("screen", [
    named(),                                                       # busy: spinner above the box
    named(busy=False),                                             # idle
    named("\x1b[38;5;241m❯\u00a0\x1b[2mcheck the soak\x1b[0m\x1b[39m"),  # a suggestion
])
def test_named_session_empty_box(screen):
    assert input_is_empty(screen)


@pytest.mark.parametrize("screen", [
    named("\x1b[39m❯\u00a0please look at the failing test"),       # typed text, busy
    named("❯\u00a0wait", busy=False),                             # typed text, idle
])
def test_named_session_typed_text(screen):
    assert input_box(screen) is not None
    assert not input_is_empty(screen)


def test_named_session_is_typed_into():
    fake = FakeTmux([named(busy=False)])
    assert TmuxAdapter("main", socket="agents-test", run=fake).deliver(DELIVERY) == DELIVERED
    assert fake.sent()[0][-1] == "[waypoint] Reminder: W-1 (soak) next line"


@pytest.mark.parametrize("line", [
    "──────── Session Name ─",
    "  ──────────────── agent-a ──────  ",
    "─" * 40,
])
def test_rules(line):
    assert _RULE.match(line)


@pytest.mark.parametrize("line", [
    "──────── Section heading",              # dashes, then text: not a rule
    "──────── notes ─ and more text",
    "──────── " + "x" * 61 + " ─",           # a label longer than any session name
    "────────  ─",                           # no label text
    "─── short ─",                           # too few dashes in front
    "text ──────── text ─",
])
def test_not_rules(line):
    assert not _RULE.match(line)


def test_dashes_in_output_above_the_box_dont_confuse_it():
    screen = "──────── Results\n" + named(busy=False)
    assert input_is_empty(screen)
    only_one_rule = "──────── Results\n❯\u00a0\n" + RULE + "\n"
    assert input_box(only_one_rule) is None


# ---------------------------------------------------------------------- busy panes and lost Enters

from waypoint.adapters import BUSY, CLEARED, box_text, pane_busy, queued  # noqa: E402

ORANGE, SALMON = "\x1b[38;5;174m", "\x1b[38;5;216m"
TIP = f"{DIMGREY}  ⎿  Tip: Use /btw to ask a quick side question{RESET}"


def status_pane(status: str | None, box: str = f"{DIMGREY}❯ {RESET}", tip: bool = True) -> str:
    """A pane in Claude Code's layout with a status line above the input box (shapes as seen in real
    panes: a glyph, a verb, and while a turn runs "…" with the elapsed time)."""
    lines = ["● Done. The tests pass.", ""]
    if status is not None:
        lines.append(status)
        if tip:
            lines.append(TIP)
    lines += ["", RULE, box, RULE, f"{RESET}  {DIMGREY}Model · ctx 12%{RESET}"]
    return "\n".join(lines)


BUSY_STATUS = [
    f"{ORANGE}✢{RESET} {SALMON}Hyperspacing…{ORANGE} {DIMGREY}(51s · ↓{RESET} {DIMGREY}3.2k tokens){RESET}",
    f"{ORANGE}✻{RESET} {ORANGE}Smooshing… {DIMGREY}(3m 4s · ↓{RESET} {DIMGREY}3.4k tokens · thinking with high effort){RESET}",
    f"{ORANGE}·{RESET} {SALMON}Ebbing…{ORANGE} {DIMGREY}(10s · ↓{RESET} {DIMGREY}878 tokens){RESET}",
    f"{ORANGE}✶{RESET} {SALMON}Tinkering…{ORANGE} {DIMGREY}(1h 2m 11s · ↓{RESET} {DIMGREY}5.2k tokens){RESET}",
    "✳ Working… (esc to interrupt)",
]
IDLE_STATUS = [
    f"{ORANGE}✻{RESET} {DIMGREY}Worked for 26s · done 1:06 PM{RESET}",
    "✻ Sautéed for 7m 35s · done 1:03 PM · 1 shell still running",
    None,
]


@pytest.mark.parametrize("status", BUSY_STATUS)
def test_busy_panes(status):
    for tip in (True, False):
        screen = status_pane(status, tip=tip)
        assert input_is_empty(screen) and pane_busy(screen)


@pytest.mark.parametrize("status", IDLE_STATUS)
def test_idle_panes(status):
    screen = status_pane(status)
    assert input_is_empty(screen) and not pane_busy(screen)


def test_the_real_named_capture_is_busy():
    assert pane_busy(named()) and not pane_busy(named(busy=False))


def test_older_output_with_dots_is_not_a_status_line():
    screen = "\n".join(["● Waiting… (this takes a while)", "", "● Done.", "", "", "",
                        status_pane(IDLE_STATUS[0])])
    assert not pane_busy(screen)


@pytest.mark.parametrize("screens", [[status_pane(BUSY_STATUS[0])],
                                     [status_pane(IDLE_STATUS[0]), status_pane(BUSY_STATUS[1])]])
def test_a_busy_session_is_not_typed_into(screens):
    fake = FakeTmux(screens)
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == BUSY
    assert fake.sent() == []


def test_typed_into_an_idle_session():
    fake = FakeTmux([status_pane(IDLE_STATUS[1])])
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == DELIVERED
    assert [a[-1] for a in fake.sent()] == ["[waypoint] Reminder: W-1 (soak) next line", "Enter"]


TYPED_LINE = "[waypoint] Reminder: W-1 (soak) next line"
LOOKS = int(TmuxAdapter.DEFAULT_WATCH)  # looks per watch when the settle wait is 0 (as in tests)


def test_enter_that_didnt_take_is_sent_once_more():
    idle = status_pane(IDLE_STATUS[0])
    stuck = status_pane(IDLE_STATUS[0], box=f"❯ {TYPED_LINE}")
    fake = FakeTmux([idle, idle, *[stuck] * LOOKS, idle])  # still there after the whole watch
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == DELIVERED
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter", "Enter"]


def test_a_line_that_stays_in_the_box_is_reported_not_delivered():
    idle = status_pane(IDLE_STATUS[0])
    # Claude Code wraps a long input over several lines; it is still our line
    stuck = status_pane(IDLE_STATUS[0], box="❯ [waypoint] Reminder: W-1\n  (soak) next line")
    fake = FakeTmux([idle, idle, stuck, stuck])
    got = TmuxAdapter("main", run=fake).deliver(DELIVERY)
    assert isinstance(got, Undeliverable) and got.reason == "typed, but not submitted"
    # never more than one extra Enter; the clear is tried once per box line and once more (the box
    # never changed), then nothing more is sent
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter", "Enter", "C-u", "C-u", "C-u"]


def test_a_clear_that_doesnt_empty_the_box_stops_there(caplog):
    idle = status_pane(IDLE_STATUS[0])
    stuck = stuck_box(TYPED_LINE)
    fake = FakeTmux([idle, idle, stuck])
    got = TmuxAdapter("main", run=fake).deliver(DELIVERY)
    assert isinstance(got, Undeliverable) and got.reason == "typed, but not submitted"
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter", "Enter", "C-u", "C-u"]
    assert "left alone" in caplog.text


def test_other_text_in_the_box_after_enter_is_no_sign_of_taken():
    """The box should be empty once the session took the line. Someone's text there instead is no
    evidence either way: reported, not called delivered, and nothing more is sent."""
    idle = status_pane(IDLE_STATUS[0])
    other = status_pane(IDLE_STATUS[0], box="❯ something else")
    fake = FakeTmux([idle, idle, other])
    got = TmuxAdapter("main", run=fake).deliver(DELIVERY)
    assert isinstance(got, Undeliverable) and got.reason == "typed, but not confirmed"
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter"]


def test_box_text():
    assert box_text(status_pane(None, box="❯ hello\n  world")) == "hello world"
    assert box_text(status_pane(None)) == ""
    assert box_text(NO_BOX) is None


def test_the_settle_wait_is_used():
    waits = []
    fake = FakeTmux([status_pane(IDLE_STATUS[0])])
    TmuxAdapter("main", run=fake, settle=1.0, sleep=waits.append).deliver(DELIVERY)
    assert waits == [1.0, 1.0]  # one look until the line shows, one after Enter


def test_busy_with_text_in_the_box_is_busy():
    """Text typed during a turn sits in the box until it ends: that is busy, not blocked."""
    screen = status_pane(BUSY_STATUS[0], box="❯\u00a0something typed while it works")
    fake = FakeTmux([screen])
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == BUSY
    assert fake.sent() == []


def test_our_line_with_someone_elses_text_after_it_is_not_submitted():
    idle = status_pane(IDLE_STATUS[0])
    added = status_pane(IDLE_STATUS[0], box=f"❯\u00a0{TYPED_LINE} and more")
    fake = FakeTmux([idle, idle, added])
    got = TmuxAdapter("main", run=fake).deliver(DELIVERY)
    assert isinstance(got, Undeliverable) and got.reason == "typed, but not submitted"
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter"]  # no Enter on someone else's text


def test_our_line_is_recognised_however_it_wraps():
    idle = status_pane(IDLE_STATUS[0])
    wrapped = status_pane(IDLE_STATUS[0], box="❯\u00a0[waypoint] Reminder: W-1 (soak) ne\n  xt line")
    fake = FakeTmux([idle, idle, *[wrapped] * LOOKS, idle])
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == DELIVERED
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter", "Enter"]



# ---------------------------------------------------------------------- lines left unsent

def stuck_box(line: str) -> str:
    return status_pane(IDLE_STATUS[0], box=f"❯\u00a0{line}")


def test_a_line_not_taken_after_two_enters_is_cleared_and_tried_again(caplog):
    idle = status_pane(IDLE_STATUS[0])
    fake = FakeTmux([idle, idle, *[stuck_box(TYPED_LINE)] * (2 * LOOKS + 1), idle])
    adapter = TmuxAdapter("main", run=fake)
    assert adapter.deliver(DELIVERY) == CLEARED  # nothing left behind; typed again after a minute
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter", "Enter", "C-u"]
    assert "D-7's line wasn't taken; pressing Enter once more" in caplog.text
    assert "cleared D-7's line after two Enters; trying again in a minute" in caplog.text
    assert TYPED_LINE not in caplog.text  # the log names the case, not the text


def test_an_earlier_line_of_ours_is_never_cleared():
    """Only the attempt that typed a line may clear it: a line left by an earlier attempt, an earlier
    deliverer process or the Up arrow blocks the box like anyone's text."""
    idle = status_pane(IDLE_STATUS[0])
    adapter = TmuxAdapter("main", run=FakeTmux([idle]))
    assert adapter.deliver(DELIVERY) == DELIVERED
    fake = FakeTmux([stuck_box(TYPED_LINE)])  # its Enter was lost after all, or it was recalled
    adapter._run = fake
    assert adapter.deliver(dict(DELIVERY, id="D-8", text="Reminder: W-2 (deploy)")) == RETRY
    assert adapter.deliver(DELIVERY) == RETRY  # its own message too: not typed again, not cleared
    assert fake.sent() == []


@pytest.mark.parametrize("box,mode", [
    ("please look at the failing test", "ref"),                                  # a person's text
    ("[waypoint] Reminder: W-1 (soak) next line and more", "full"),              # ours plus someone's
    ("[waypoint] wake-up for W-17 (reminder): read it with wp_get W-17 thanks", "ref"),
    ("[waypoint] wake-up for W-17 (reminder): read it with wp_get W-17", "full"),  # a full-mode adapter
                                                                                  # doesn't know it
    ("[waypoint] Reminder: W-1 (soak) next line", "full"),                       # not typed by this attempt
    ("[waypoint] wake-up for W-17 (reminder): read it with wp_get W-17", "ref"),   # a ref line, after a restart
])
def test_other_text_is_never_touched(box, mode):
    fake = FakeTmux([stuck_box(box)])
    assert TmuxAdapter("main", run=fake, message=mode).deliver(DELIVERY) == RETRY
    assert fake.sent() == []  # no Ctrl-U, nothing typed: delivery stays blocked


def test_a_busy_pane_with_our_line_is_left_alone():
    adapter = TmuxAdapter("main", run=FakeTmux([status_pane(IDLE_STATUS[0])]))
    adapter.deliver(DELIVERY)
    fake = FakeTmux([status_pane(BUSY_STATUS[0], box=f"❯\u00a0{TYPED_LINE}")])
    adapter._run = fake
    assert adapter.deliver(DELIVERY) == BUSY and fake.sent() == []



# ---------------------------------------------------------------------- after a clear

def test_repeats_for_one_message_are_logged_first_and_last(caplog):
    idle = status_pane(IDLE_STATUS[0])
    stuck = stuck_box(TYPED_LINE)
    adapter = TmuxAdapter("main", run=FakeTmux([idle]))
    for _ in range(3):
        adapter._run = FakeTmux([idle, idle, *[stuck] * (2 * LOOKS + 1), idle])
        assert adapter.deliver(DELIVERY) == CLEARED
    assert caplog.text.count("pressing Enter once more") == 1
    assert caplog.text.count("cleared D-7's line") == 1
    adapter.settled("D-7")
    assert "D-7's line wasn't taken 3 times in all" in caplog.text
    adapter.settled("D-7")  # nothing more to say
    assert caplog.text.count("times in all") == 1


@pytest.mark.parametrize("line", [
    "[waypoint] wake-up for W-12 (reminder): read it with wp_get W-99",   # the ids differ
    "[waypoint] wake-up for W-12 (reminder): read it with wp_get W-123",
])
def test_a_ref_line_must_name_one_item(line):
    fake = FakeTmux([stuck_box(line)])
    assert TmuxAdapter("main", run=fake, message="ref").deliver(DELIVERY) == RETRY
    assert fake.sent() == []


# ---------------------------------------------------------------------- a pane that redraws late, a busy one

QUEUED_LINE = (f"{DIMGREY}\x1b[48;5;255m❯ {TYPED_LINE}{RESET}\n"
               f"{DIMGREY}\x1b[49m  ⎿  Queued · ctrl+x ctrl+s to send now{RESET}")
# the box of a busy pane with a queued message (shape from a real pane): a dim hint only
QUEUED_HINT = f"{DIMGREY}❯\u00a0\x1b[2m{RESET}Press up to edit queued messages\x1b[0m"


def queued_pane(box: str = QUEUED_HINT) -> str:
    return "\n".join(["● Still working on it.", "", QUEUED_LINE, "", status_pane(BUSY_STATUS[0], box=box)])


def test_a_pane_that_redraws_late_is_waited_for(caplog):
    """The session took the line at once, but a loaded machine redrew the pane seconds later: one
    Enter, no "not taken", no clear (seen twice in one night)."""
    idle = status_pane(IDLE_STATUS[0])
    late = stuck_box(TYPED_LINE)
    fake = FakeTmux([idle, idle, late, late, late, idle])
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == DELIVERED
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter"]
    assert "wasn't taken" not in caplog.text


def test_a_turn_that_starts_after_enter_means_taken():
    """Idle when typed, busy after Enter with our line gone from the box: the turn is ours."""
    idle = status_pane(IDLE_STATUS[0])
    fake = FakeTmux([idle, idle, status_pane(BUSY_STATUS[0], box="❯ something typed since")])
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == DELIVERED
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter"]


def test_a_turn_with_our_line_still_in_the_box_is_not_ours():
    """Claude Code draws the status line and the box in one frame: busy with our line still in the box
    means some other turn. Not delivered, and no second Enter into a busy pane."""
    idle = status_pane(IDLE_STATUS[0])
    fake = FakeTmux([idle, idle, status_pane(BUSY_STATUS[0], box=f"❯ {TYPED_LINE}")])
    got = TmuxAdapter("main", run=fake).deliver(DELIVERY)
    assert isinstance(got, Undeliverable) and got.reason == "typed, but not confirmed"
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter"]


def test_a_queued_line_is_taken():
    idle = status_pane(IDLE_STATUS[0])
    fake = FakeTmux([idle, idle, queued_pane()])
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == DELIVERED
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter"]
    assert input_is_empty(queued_pane()) and pane_busy(queued_pane())


def test_queued_names_only_that_line():
    assert queued(queued_pane(), TYPED_LINE)
    assert queued(queued_pane(), TYPED_LINE.replace(" ", "  "))  # wrapped or not
    assert not queued(queued_pane(), "[waypoint] Reminder: W-2 (deploy)")
    assert not queued(status_pane(BUSY_STATUS[0]), TYPED_LINE)
    assert not queued(NO_BOX, TYPED_LINE)
    wrapped = queued_pane().replace(f"❯ {TYPED_LINE}", "❯ [waypoint] Reminder: W-1\n  (soak) next line")
    assert queued(wrapped, TYPED_LINE)


def test_the_watch_looks_every_settle_for_the_watch_time():
    waits = []
    idle = status_pane(IDLE_STATUS[0])
    fake = FakeTmux([idle, idle, stuck_box(TYPED_LINE)])
    TmuxAdapter("main", run=fake, settle=1.0, watch=8.0, sleep=waits.append).deliver(DELIVERY)
    # one look until the line shows, two whole watches, and a look after each C-u (one box line: two)
    assert waits == [1.0] * 19


@pytest.mark.parametrize("modes, keys", [
    (["0", "0", "1"], [TYPED_LINE]),                              # scrolled back while it showed
    (["0", "0", "0", "1"], [TYPED_LINE, "Enter"]),                # ...during the first watch
    (["0", "0", "0", "0", "1"], [TYPED_LINE, "Enter", "Enter"]),  # ...or during the second
])
def test_no_key_goes_into_a_mode_after_typing(modes, keys, caplog):
    """capture-pane shows the normal screen in copy mode, so the mode is asked again before the
    second Enter and before the C-u: an Enter there would end a person's copy mode."""
    idle = status_pane(IDLE_STATUS[0])
    fake = FakeTmux([idle, idle, stuck_box(TYPED_LINE)], modes=modes)
    got = TmuxAdapter("main", run=fake).deliver(DELIVERY)
    assert isinstance(got, Undeliverable) and got.reason == "typed, but not submitted"
    assert [a[-1] for a in fake.sent()] == keys  # no Enter and no C-u into the mode
    assert "the pane is in a mode; left alone" in caplog.text



# ---------------------------------------------------------------------- taken only on evidence

def test_a_line_that_never_shows_gets_no_enter_and_holds_the_pane(caplog):
    """Two lines were logged delivered while both sat unsent in an idle session's box, the second
    added after the first. The box hadn't shown them yet, so "the box doesn't start with our line"
    read as taken. Now: no Enter on a line the box never showed, report it, and type nothing more
    into that pane for a while."""
    now = [1000.0]
    idle = status_pane(IDLE_STATUS[0])
    fake = FakeTmux([idle], echo=False)
    adapter = TmuxAdapter("main", run=fake, clock=lambda: now[0])
    got = adapter.deliver(DELIVERY)
    assert isinstance(got, Undeliverable) and got.reason == "typed, but it didn't show in the box"
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE]  # no Enter
    assert "D-7's line didn't show in the box" in caplog.text and TYPED_LINE not in caplog.text
    # the next message for that pane: the box still looks empty, but nothing is typed
    assert adapter.deliver(dict(DELIVERY, id="D-8", text="W-9 is due")) == RETRY
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE]
    now[0] += TmuxAdapter.HOLD  # the line was lost after all: the pane is free again
    fake.echo, fake.typed = True, None
    assert adapter.deliver(dict(DELIVERY, id="D-8", text="W-9 is due")) == DELIVERED


def test_a_line_that_shows_late_is_waited_for_before_enter():
    idle = status_pane(IDLE_STATUS[0])
    fake = FakeTmux([idle], late=LOOKS - 1)
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == DELIVERED
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter"]
    assert sum(1 for a in fake.calls if "capture-pane" in a) == 2 + LOOKS + 1  # ready, shows, watch


def test_other_text_with_ours_before_enter_gets_no_enter():
    idle = status_pane(IDLE_STATUS[0])
    fake = FakeTmux([idle], shows=f"{TYPED_LINE} and more")
    got = TmuxAdapter("main", run=fake).deliver(DELIVERY)
    assert isinstance(got, Undeliverable) and got.reason == "typed, but not submitted"
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE]


@pytest.mark.parametrize("placeholder", ["[Pasted text #1]", "[Pasted text #12 +3 lines]"])
def test_a_long_line_shown_as_pasted_text_is_ours(placeholder):
    """Claude Code shows a long typed line as "[Pasted text #1]" (seen on 2.1.330 with a 2300-character
    line). That is our line, before and after Enter."""
    idle = status_pane(IDLE_STATUS[0])
    fake = FakeTmux([idle], shows=placeholder)
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == DELIVERED
    stuck = stuck_box(placeholder)
    fake = FakeTmux([idle, idle, *[stuck] * (2 * LOOKS + 1), idle], shows=placeholder)
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == CLEARED
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter", "Enter", "C-u"]


def test_an_enter_that_became_a_line_break_gets_one_more():
    """An Enter that ends up in the box as a line break leaves our line and an empty line: still our
    line, so after the watch it gets the second Enter, not "taken"."""
    idle = status_pane(IDLE_STATUS[0])
    broken = status_pane(IDLE_STATUS[0], box=f"❯ {TYPED_LINE}\n  ")
    fake = FakeTmux([idle, idle, *[broken] * LOOKS, idle])
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == DELIVERED
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter", "Enter"]


def test_a_pane_that_cant_be_read_after_enter_is_not_taken():
    idle = status_pane(IDLE_STATUS[0])
    fake = FakeTmux([idle])
    real = fake.__call__
    entered = []

    def run(args):
        if args[-1] == "Enter":
            entered.append(1)
        if entered and "capture-pane" in args:
            return 1, ""
        return real(args)

    got = TmuxAdapter("main", run=run).deliver(DELIVERY)
    assert isinstance(got, Undeliverable) and got.reason == "typed, but not confirmed"


def test_one_line_leaves_out_invisible_characters():
    assert one_line("a​b⁠c﻿d‮e\U000e0041f g️ h‍ㅤi") == "abcdef g hi"
    assert one_line("wake-up for W-1   read it") == "wake-up for W-1 read it"


def test_a_line_with_invisible_characters_matches_the_box():
    """The box never shows invisible characters, so a typed line holding one could never be seen as
    ours; they are left out before typing."""
    idle = status_pane(IDLE_STATUS[0])
    fake = FakeTmux([idle])
    assert TmuxAdapter("main", run=fake).deliver(dict(DELIVERY, text="Reminder:​ W-1")) == DELIVERED
    assert fake.sent()[0][-1] == "[waypoint] Reminder: W-1"


def test_one_line_is_composed_as_the_box_shows_it():
    """The box shows composed text: "e" + a combining accent typed as two characters would never
    match it, so the line is composed (NFC) before typing."""
    assert one_line("cafe\u0301 Ame\u0301lie Zu\u0308rich") == "caf\u00e9 Am\u00e9lie Z\u00fcrich"
    assert one_line("\u1112\u1161\u11ab") == "\ud55c"  # Hangul jamo -> one syllable


def test_a_decomposed_line_is_delivered():
    idle = status_pane(IDLE_STATUS[0])
    fake = FakeTmux([idle])  # the fake box shows what was typed, already composed
    assert TmuxAdapter("main", run=fake).deliver(dict(DELIVERY, text="Reminder: cafe\u0301")) == DELIVERED
    assert fake.sent()[0][-1] == "[waypoint] Reminder: caf\u00e9"


def test_a_box_that_shows_the_start_of_our_line_is_waited_for():
    """A box drawn part way shows only the start of our line: that is not other text."""
    idle = status_pane(IDLE_STATUS[0])
    part = with_box(idle, TYPED_LINE[:15])
    fake = FakeTmux([idle, idle, idle])
    real = fake.__call__
    looks = []

    def run(args):
        if "capture-pane" in args and fake.typed is not None:
            looks.append(1)
            if len(looks) <= 2:
                return 0, part
        return real(args)

    assert TmuxAdapter("main", run=run).deliver(DELIVERY) == DELIVERED
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter"]


def test_a_box_that_never_shows_more_than_the_start_is_like_never_shown():
    idle = status_pane(IDLE_STATUS[0])
    fake = FakeTmux([idle], shows=TYPED_LINE[:15])
    got = TmuxAdapter("main", run=fake).deliver(DELIVERY)
    assert isinstance(got, Undeliverable) and got.reason == "typed, but it didn't show in the box"
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE]


# ---------------------------------------------------------------------- clearing a wrapped line

def test_a_wrapped_line_is_cleared_one_screen_line_at_a_time():
    """Claude Code's C-u deletes back to the start of a screen line only: a line wrapped over three
    takes three (seen with a 300-character CJK line)."""
    idle = status_pane(IDLE_STATUS[0])
    three = status_pane(IDLE_STATUS[0], box="❯\u00a0[waypoint] Reminder:\n  W-1 (soak)\n  next line")
    two = status_pane(IDLE_STATUS[0], box="❯\u00a0[waypoint] Reminder:\n  W-1 (soak)")
    one = status_pane(IDLE_STATUS[0], box="❯\u00a0[waypoint] Reminder:")
    fake = FakeTmux([idle, idle, *[three] * (2 * LOOKS), three, two, one, idle])
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == CLEARED
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter", "Enter", "C-u", "C-u", "C-u"]


def test_the_clear_stops_at_text_that_isnt_ours(caplog):
    idle = status_pane(IDLE_STATUS[0])
    three = status_pane(IDLE_STATUS[0], box="❯\u00a0[waypoint] Reminder:\n  W-1 (soak)\n  next line")
    other = status_pane(IDLE_STATUS[0], box="❯\u00a0[waypoint] Reminder: please wait")
    fake = FakeTmux([idle, idle, *[three] * (2 * LOOKS), three, other])
    got = TmuxAdapter("main", run=fake).deliver(DELIVERY)
    assert isinstance(got, Undeliverable) and got.reason == "typed, but not submitted"
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter", "Enter", "C-u"]
    assert "left alone" in caplog.text


def test_the_clear_sends_no_key_into_a_mode():
    idle = status_pane(IDLE_STATUS[0])
    three = status_pane(IDLE_STATUS[0], box="❯\u00a0[waypoint] Reminder:\n  W-1 (soak)\n  next line")
    two = status_pane(IDLE_STATUS[0], box="❯\u00a0[waypoint] Reminder:\n  W-1 (soak)")
    # modes: two looks before typing, before the first Enter, after each watch; then scrolled back
    fake = FakeTmux([idle, idle, *[three] * (2 * LOOKS), three, two], modes=["0"] * 5 + ["1"])
    got = TmuxAdapter("main", run=fake).deliver(DELIVERY)
    assert isinstance(got, Undeliverable) and got.reason == "typed, but not submitted"
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter", "Enter", "C-u"]


def test_the_clear_sends_nothing_unless_the_box_holds_exactly_our_line(caplog):
    """Right before the first C-u the box is looked at once more: someone's text there (also after
    our line was taken late) is never cleared, not even by the first C-u."""
    idle = status_pane(IDLE_STATUS[0])
    stuck = stuck_box(TYPED_LINE)
    for before in (status_pane(IDLE_STATUS[0], box="❯ a person's question"),
                   status_pane(IDLE_STATUS[0], box=f"❯ {TYPED_LINE} and more"),
                   idle):
        fake = FakeTmux([idle, idle, *[stuck] * (2 * LOOKS), before])
        got = TmuxAdapter("main", run=fake).deliver(DELIVERY)
        assert isinstance(got, Undeliverable) and got.reason == "typed, but not submitted"
        assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter", "Enter"]  # no C-u
