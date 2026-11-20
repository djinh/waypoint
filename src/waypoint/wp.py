"""wp: Waypoint on the command line, for scripts and humans.

    wp ls [--owner X] [--track T] [--state S] [--waiting-on X] [--late] [--at-risk]
    wp mine
    wp show W-12
    wp add "Title" --track T [--kind K] [--owner O] [--label L] [--waits-on W-3 ...] [--due TIME] ...
    wp update W-12 [--state S] [--note N] [--due TIME] [--add-waits-on W-3] ...
    wp done W-12 "closing note"          wp drop W-12 "reason"
    wp ask alice "Title" "Question?" [--option A --option B] [--recommend A] [--for W-12]
    wp tracks                            wp track put ID --title T [--lead L] [--order N] [--note N]
    wp client ls | add NAME --role R --token-file F [--label L] [--deliverer D]
              | rotate NAME --token-file F | route NAME DELIVERER|- | retire NAME
    wp health

Settings: --url / WAYPOINT_URL (default http://127.0.0.1:7130), --token-file / WAYPOINT_TOKEN_FILE
(default ~/.config/waypoint/tokens/<name>.token), --name / WAYPOINT_NAME (for `mine` and the
default token file). Add --json for the API's answer as JSON.

Exit codes: 0 done, 1 Waypoint refused it, 2 usage or input, 3 Waypoint unavailable.
Tokens are written only to new files readable by their owner, never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .client import ApiFailure, Unavailable, WaypointClient, read_token
from .present import (InputError, answer_line, client_name, flat, item_id, line, local, to_condition,
                      to_ts, to_when)
from .schema import limits
from .server import issue_token

DEFAULT_URL = "http://127.0.0.1:7130"


class Usage(Exception):
    pass


def parser() -> argparse.ArgumentParser:
    L = limits()
    p = argparse.ArgumentParser(
        prog="wp", description="Waypoint on the command line",
        epilog=f"Limits: title {L['title']} characters, label {L['label']}, note {L['note']}, "
               f"question {L['question']}, options {L['options']} of {L['option']} each, "
               f"recommendation {L['recommendation']}, links {L['links']}, waits-on {L['waits_on']}.")
    p.add_argument("--url", default=os.environ.get("WAYPOINT_URL", DEFAULT_URL))
    p.add_argument("--token-file", default=os.environ.get("WAYPOINT_TOKEN_FILE"))
    p.add_argument("--name", default=os.environ.get("WAYPOINT_NAME"))
    p.add_argument("--json", action="store_true", help="print the API's answer as JSON (errors too)")
    p.add_argument("--version", action="version", version=f"wp {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("ls", help="list items")
    ls.add_argument("--owner")
    ls.add_argument("--track")
    ls.add_argument("--state", help="comma-separated states, or all")
    ls.add_argument("--waiting-on")
    ls.add_argument("--late", action="store_true")
    ls.add_argument("--at-risk", action="store_true")
    sub.add_parser("mine", help="my open items")
    show = sub.add_parser("show", help="one item in full")
    show.add_argument("id")

    def item_fields(sp, create: bool):
        sp.add_argument("--owner")
        sp.add_argument("--label", help=f"short form, at most {L['label']} characters; '' clears")
        sp.add_argument("--note", help=f"one line of status, at most {L['note']} characters; '' clears")
        sp.add_argument("--due", help="a time; local if no zone; '' clears")
        sp.add_argument("--wake", help="a time; local if no zone; '' clears")
        sp.add_argument("--when", help="a day (2026-03-12) or a time; '' clears")
        sp.add_argument("--until")
        sp.add_argument("--link", action="append", default=[])
        sp.add_argument("--waits-on", action="append", default=None,
                        help="an item id or a time; repeatable; replaces all conditions ('' clears them)")
        sp.add_argument("--running-until", metavar="TIME",
                        help="running until this time (local if no zone); on add it also sets running")
        if create:
            sp.add_argument("--track", required=True)
            sp.add_argument("--kind", default="task", choices=["task", "decision", "approval", "event"])
        else:
            sp.add_argument("--add-waits-on", action="append", default=[])
            sp.add_argument("--remove-waits-on", action="append", default=[], metavar="ID_OR_TIME",
                            help="drop one condition, keeping the others; repeatable")
            where = sp.add_mutually_exclusive_group()
            where.add_argument("--answer-issue", metavar="URL", help="answered as a comment on this issue or PR")
            where.add_argument("--answer-session", metavar="NAME", help="answered in this session (client name)")
            where.add_argument("--answer-default", action="store_true",
                               help="back to the default: the session that asked")
            sp.add_argument("--state", choices=["running", "next", "waiting", "done", "dropped"])
            sp.add_argument("--title", help=f"one line, at most {L['title']} characters")
            sp.add_argument("--track")

    add = sub.add_parser("add", help="add an item")
    add.add_argument("title", help=f"one line, at most {L['title']} characters")
    item_fields(add, create=True)
    upd = sub.add_parser("update", help="change an item")
    upd.add_argument("id")
    item_fields(upd, create=False)
    done = sub.add_parser("done", help="mark an item done (also a person's question you asked: quote the answer "
                                       "and say where it was given)")
    done.add_argument("id")
    done.add_argument("note", help=f"closing note, at most {L['note']} characters")
    drop = sub.add_parser("drop", help="drop an item, with the reason")
    drop.add_argument("id")
    drop.add_argument("reason", help=f"at most {L['note']} characters")
    ask = sub.add_parser("ask", help="put a decision to a human")
    ask.add_argument("human")
    ask.add_argument("title", help=f"at most {L['title']} characters")
    ask.add_argument("question", help=f"at most {L['question']} characters")
    ask.add_argument("--option", action="append", default=[],
                     help=f"repeatable: at most {L['options']}, each at most {L['option']} characters")
    ask.add_argument("--recommend", help=f"at most {L['recommendation']} characters")
    ask.add_argument("--for", dest="for_item")
    ask.add_argument("--track")
    ask.add_argument("--due")
    ask.add_argument("--link", action="append", default=[], help="repeatable: http(s) URL")
    ask_where = ask.add_mutually_exclusive_group()
    ask_where.add_argument("--answer-issue", metavar="URL",
                           help="answered as a comment on this issue or PR (needed if --link names one)")
    ask_where.add_argument("--answer-session", metavar="NAME", help="answered in this session (client name)")

    sub.add_parser("tracks", help="list tracks")
    tr = sub.add_parser("track", help="create or replace a track").add_subparsers(dest="action", required=True)
    put = tr.add_parser("put")
    put.add_argument("id")
    put.add_argument("--title", required=True)
    put.add_argument("--lead")
    put.add_argument("--note")
    put.add_argument("--order", type=int, default=0)
    put.add_argument("--archived", action=argparse.BooleanOptionalAction, default=None,
                     help="archive or unarchive it (default: as it is)")

    cl = sub.add_parser("client", help="manage clients (coordinators and humans)").add_subparsers(
        dest="action", required=True)
    cl.add_parser("ls")
    cadd = cl.add_parser("add")
    cadd.add_argument("client")
    cadd.add_argument("--role", required=True, choices=["agent", "human", "coordinator", "reader", "deliverer"])
    cadd.add_argument("--label")
    cadd.add_argument("--deliverer")
    cadd.add_argument("--token-file", dest="new_token_file", required=True, help="new file for the token")
    rot = cl.add_parser("rotate")
    rot.add_argument("client")
    rot.add_argument("--token-file", dest="new_token_file", required=True, help="new file for the token")
    route = cl.add_parser("route")
    route.add_argument("client")
    route.add_argument("deliverer", help="a deliverer's name, or - for none")
    ret = cl.add_parser("retire")
    ret.add_argument("client")
    sub.add_parser("health")
    return p


def make_client(args) -> WaypointClient:
    path = args.token_file or (os.path.expanduser(f"~/.config/waypoint/tokens/{args.name}.token")
                               if args.name else None)
    if not path:
        raise Usage("no token: give --token-file or WAYPOINT_TOKEN_FILE (or WAYPOINT_NAME)")
    try:
        token = read_token(path)
    except (OSError, ValueError):
        raise Usage("cannot read the token file") from None
    return WaypointClient(args.url, token, timeout=90)


def answer_arg(args) -> dict | None:
    if getattr(args, "answer_issue", None):
        return {"issue": args.answer_issue.strip()}
    if getattr(args, "answer_session", None):
        return {"session": client_name(args.answer_session)}
    return None


def item_body(args, create: bool) -> dict:
    body: dict = {}
    if create:
        body.update(title=args.title, track=args.track, kind=args.kind)
    else:
        for key in ("state", "title", "track"):
            if getattr(args, key):
                body[key] = getattr(args, key)
    if args.owner:
        body["owner"] = args.owner
    for key in ("label", "note"):
        val = getattr(args, key)
        if val is not None:
            body[key] = val or None
    for key, field in (("due", "due_at"), ("wake", "wake_at")):
        val = getattr(args, key)
        if val is not None:
            body[field] = to_ts(val) if val else None
    if args.running_until:
        body["running_until"] = to_ts(args.running_until)
        if create:
            body["state"] = "running"
    if args.when is not None:
        body["when"] = to_when(args.when, args.until) if args.when else None
    if args.link:
        body["links"] = [{"url": u} for u in args.link]
    if args.waits_on is not None:
        body["waits_on"] = [to_condition(w) for w in args.waits_on if w.strip()]
    if not create and (args.answer_issue or args.answer_session or args.answer_default):
        body["answer_at"] = answer_arg(args)
    if not create and args.add_waits_on:
        body["add_waits_on"] = [to_condition(w) for w in args.add_waits_on]
    if create:
        body = {k: v for k, v in body.items() if v is not None}
    return body


def _dumps(value, indent=None) -> str:
    """JSON with non-ASCII text as it is, except the line and paragraph separators (U+2028/9), which
    terminals and some readers take as line breaks: they stay escaped."""
    return json.dumps(value, indent=indent, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def run(args, out) -> int:
    if args.cmd == "health":
        got = WaypointClient(args.url, "", timeout=10).request("GET", "/v1/health")
        print(_dumps(got) if args.json else f"{got['status']} {got['version']}", file=out)
        return 0
    client = make_client(args)

    def api(*a, **kw):
        got = client.request(*a, **kw)
        return got if args.json else flat(got)  # text output: other clients' text stays on its line

    def show(got, text):
        print(_dumps(got, indent=2) if args.json else text, file=out)

    if args.cmd in ("ls", "mine"):
        if args.cmd == "mine":
            if not args.name:
                raise Usage("wp mine needs WAYPOINT_NAME or --name")
            q = {"owner": args.name}
        else:
            q = {"owner": args.owner, "track": args.track, "state": args.state, "waiting_on": args.waiting_on,
                 "late": "true" if args.late else None, "at_risk": "true" if args.at_risk else None}
        got = api("GET", "/v1/items", query=q)
        text = "\n".join(line(it) for it in got["items"]) or "No items."
        if got["truncated"]:
            text += "\n(more; narrow the filter)"
        show(got, text)
    elif args.cmd == "show":
        it = api("GET", f"/v1/items/{item_id(args.id)}")
        rows = [line(it)]
        if it["question"]:
            rows.append(f"question: {it['question']['text']}")
        if answer_line(it):
            rows.append(answer_line(it))
        if it["waited_on_by"]:
            rows.append("waited on by: " + ", ".join(it["waited_on_by"]))
        rows += [f"link: {link['url']}" for link in it["links"]]
        rows += [f"  {local(h['at'])} {h['by'] or 'waypoint'} {h['event']}" + (f": {h['note']}" if h["note"] else "")
                 for h in it["history"][-10:]]
        show(it, "\n".join(rows))
    elif args.cmd == "add":
        it = api("POST", "/v1/items", item_body(args, create=True))
        show(it, line(it))
    elif args.cmd == "update":
        body = item_body(args, create=False)
        if args.remove_waits_on:
            if "waits_on" in body:
                raise Usage("give --waits-on or --remove-waits-on, not both")
            gone = [to_condition(w) for w in args.remove_waits_on]
            cur = api("GET", f"/v1/items/{item_id(args.id)}")
            kept = [{"item": c["item"]} if "item" in c else {"time": c["time"]} for c in cur["waits_on"]]
            missing = [g for g in gone if g not in kept]
            if missing:
                raise Usage(f"{item_id(args.id)} doesn't wait on {next(iter(missing[0].values()))}")
            body["waits_on"] = [c for c in kept if c not in gone]
            body["if_version"] = cur["version"]  # nobody changed the conditions in between
        if not body:
            raise Usage("nothing to change")
        it = api("PATCH", f"/v1/items/{item_id(args.id)}", body)
        show(it, line(it))
    elif args.cmd in ("done", "drop"):
        state = "done" if args.cmd == "done" else "dropped"
        it = api("PATCH", f"/v1/items/{item_id(args.id)}", {"state": state, "note": args.note if state == "done" else args.reason})
        show(it, line(it))
    elif args.cmd == "ask":
        q = {"text": args.question, "options": args.option, "recommendation": args.recommend}
        extra: dict = {}
        if answer_arg(args):
            extra["answer_at"] = answer_arg(args)
        if args.link:
            extra["links"] = [{"url": u} for u in args.link]
        if args.for_item:
            body = {"human": args.human, "title": args.title, "question": q, **extra}
            if args.track:
                body["track"] = args.track
            if args.due:
                body["due_at"] = to_ts(args.due)
            got = api("POST", f"/v1/items/{item_id(args.for_item)}/ask", body)
            show(got, line(got["asked"]) + f"\n{got['item']['id']} waits on it.")
        else:
            if not args.track:
                raise Usage("give --for (the item that waits on the answer) or --track")
            body = {"owner": args.human, "track": args.track, "title": args.title, "kind": "decision", "question": q,
                    **extra}
            if args.due:
                body["due_at"] = to_ts(args.due)
            asked = api("POST", "/v1/items", body)
            show(asked, line(asked))
    elif args.cmd == "tracks":
        got = api("GET", "/v1/tracks", query={"archived": "true"})
        show(got, "\n".join(f"{t['id']}: {t['title']} (lead {t['lead'] or '-'})" + (" [archived]" if t["archived"] else "")
                            for t in got["tracks"]) or "No tracks.")
    elif args.cmd == "track":
        archived = args.archived
        if archived is None:  # a PUT replaces the track: keep its archived state unless told
            known = {t["id"]: t for t in api("GET", "/v1/tracks", query={"archived": "true"})["tracks"]}
            archived = bool(known.get(args.id, {}).get("archived", False))
        body = {"title": args.title, "lead": args.lead, "note": args.note, "order": args.order,
                "archived": archived}
        t = api("PUT", f"/v1/tracks/{client_name(args.id)}", body)
        show(t, f"{t['id']}: {t['title']}")
    elif args.cmd == "client":
        return run_client(args, api, show)
    return 0


def run_client(args, api, show) -> int:
    def describe(c):
        bits = [f"{c['name']} ({c['role']})"]
        if c["label"]:
            bits.append(c["label"])
        if c["deliverer"]:
            bits.append(f"via {c['deliverer']}")
        if c["last_seen"]:
            bits.append(f"last fetch {local(c['last_seen'])}")
        if c["retired_at"]:
            bits.append("retired")
        return " · ".join(bits)

    if args.action == "ls":
        got = api("GET", "/v1/clients")
        show(got, "\n".join(describe(c) for c in got["clients"]) or "No clients.")
    elif args.action in ("add", "rotate"):
        if args.action == "add":
            body = {"name": args.client, "role": args.role}
            if args.label:
                body["label"] = args.label
            if args.deliverer:
                body["deliverer"] = args.deliverer
            call = lambda: api("POST", "/v1/clients", body)  # noqa: E731
        else:
            call = lambda: api("POST", f"/v1/clients/{client_name(args.client)}/rotate")  # noqa: E731
        got = {}

        def make_token():
            got.update(call())
            return got["token"]

        try:
            issue_token(args.new_token_file, make_token)
        except FileExistsError:
            raise Usage("the token file exists already; remove it or name another") from None
        except OSError:
            raise Usage("cannot write the token file") from None
        show(got["client"], describe(got["client"]) + f"\ntoken written to {args.new_token_file}")
    elif args.action == "route":
        c = api("PATCH", f"/v1/clients/{client_name(args.client)}",
                {"deliverer": None if args.deliverer == "-" else args.deliverer})
        show(c, describe(c))
    elif args.action == "retire":
        c = api("PATCH", f"/v1/clients/{client_name(args.client)}", {"retired": True})
        show(c, describe(c))
    return 0


def main(argv: list[str] | None = None, out=None, err=None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = parser().parse_args(argv)

    def fail(code: int, error: dict, text: str) -> int:
        # with --json, errors are JSON too (on stderr, like the text form), in the API's own shape
        print(_dumps({"error": error}) if args.json else f"wp: {text}", file=err)
        return code

    try:
        return run(args, out)
    except (Usage, InputError) as e:
        return fail(2, {"code": "usage", "message": str(e)}, str(e))
    except Unavailable:
        return fail(3, {"code": "unavailable", "message": "Waypoint unavailable"}, "Waypoint unavailable")
    except ApiFailure as e:
        where = f" ({e.field})" if e.field else ""
        cycle = f": {' → '.join(e.cycle)}" if e.cycle else ""
        error = {"code": e.code, "message": e.message, **({"field": e.field} if e.field else {}),
                 **({"cycle": e.cycle} if e.cycle else {})}
        return fail(1, error, f"{e.code}{where}: {e.message}{cycle}")


if __name__ == "__main__":
    sys.exit(main())
