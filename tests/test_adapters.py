"""The deliverer's adapters: tmux (empty input box only), push (summary only), webhook."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from waypoint.adapters import (_RULE, DELIVERED, RETRY, PushAdapter, TmuxAdapter, Undeliverable, WebhookAdapter,
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


class FakeTmux:
    """Records tmux calls and answers capture-pane from a list of screens, and pane_in_mode from
    a list of modes."""

    def __init__(self, screens, fail=(), modes=("0",)):
        self.screens = list(screens)
        self.modes = list(modes)
        self.calls = []
        self.fail = set(fail)

    def __call__(self, args):
        self.calls.append(args)
        verb = args[3] if args[1] in ("-L", "-S") else args[1]
        if verb in self.fail:
            return 1, ""
        if verb == "capture-pane":
            return 0, self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
        if verb == "display-message":
            return 0, (self.modes.pop(0) if len(self.modes) > 1 else self.modes[0]) + "\n"
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


@pytest.fixture
def real_pane(tmp_path):
    """A real tmux server on a socket file in tmp_path, with a pane that draws an empty input box
    (with a dim suggestion) and then echoes what it gets."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    sock = str(tmp_path / "tmux.sock")
    script = tmp_path / "fake-pane.sh"
    script.write_text(
        "printf 'output\\n'\n"
        f"printf '{'─' * 60}\\n'\n"
        "printf '❯\\302\\240\\033[2mtry the tests\\033[0m\\n'\n"
        f"printf '{'─' * 60}\\n'\n"
        "exec cat\n", encoding="utf-8")
    subprocess.run(["tmux", "-S", sock, "-f", "/dev/null", "new-session", "-d", "-s", "t", "-x", "120", "-y", "20",
                    f"sh {script}"], check=True, timeout=10)
    adapter = TmuxAdapter("t", socket=sock, settle=0.3)
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
    # never more than one extra Enter; the clear is tried, the box didn't empty, nothing more is sent
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter", "Enter", "C-u"]


def test_a_clear_that_doesnt_empty_the_box_stops_there(caplog):
    idle = status_pane(IDLE_STATUS[0])
    stuck = stuck_box(TYPED_LINE)
    fake = FakeTmux([idle, idle, stuck])
    got = TmuxAdapter("main", run=fake).deliver(DELIVERY)
    assert isinstance(got, Undeliverable) and got.reason == "typed, but not submitted"
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter", "Enter", "C-u"]
    assert "left alone" in caplog.text


def test_someone_elses_text_after_ours_is_not_our_line():
    idle = status_pane(IDLE_STATUS[0])
    other = status_pane(IDLE_STATUS[0], box="❯ something else")
    fake = FakeTmux([idle, idle, other])
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == DELIVERED
    assert [a[-1] for a in fake.sent()] == [TYPED_LINE, "Enter"]


def test_box_text():
    assert box_text(status_pane(None, box="❯ hello\n  world")) == "hello world"
    assert box_text(status_pane(None)) == ""
    assert box_text(NO_BOX) is None


def test_the_settle_wait_is_used():
    waits = []
    fake = FakeTmux([status_pane(IDLE_STATUS[0])])
    TmuxAdapter("main", run=fake, settle=1.0, sleep=waits.append).deliver(DELIVERY)
    assert waits == [1.0]


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
    fake = FakeTmux([idle, idle, *[stuck_box(TYPED_LINE)] * (2 * LOOKS), idle])
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
        adapter._run = FakeTmux([idle, idle, *[stuck] * (2 * LOOKS), idle])
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
    """Idle when typed, busy after Enter: the turn is ours. No second Enter into a busy pane, even if
    the box isn't redrawn yet."""
    idle = status_pane(IDLE_STATUS[0])
    fake = FakeTmux([idle, idle, status_pane(BUSY_STATUS[0], box=f"❯\u00a0{TYPED_LINE}")])
    assert TmuxAdapter("main", run=fake).deliver(DELIVERY) == DELIVERED
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
    assert waits == [1.0] * 16  # two whole watches before the clear


@pytest.mark.parametrize("modes, keys", [
    (["0", "0", "1"], [TYPED_LINE, "Enter"]),                # scrolled back during the first watch
    (["0", "0", "0", "1"], [TYPED_LINE, "Enter", "Enter"]),  # ...or during the second
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

