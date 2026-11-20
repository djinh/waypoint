"""waypoint-mcp: Waypoint's nine MCP tools, for one agent session, over the HTTP API.

The agent's client starts it per session (stdio). Configuration comes from the environment:

    WAYPOINT_NAME        the session's client name (required)
    WAYPOINT_URL         default http://127.0.0.1:7130
    WAYPOINT_TOKEN_FILE  default ~/.config/waypoint/tokens/<WAYPOINT_NAME>.token

Every tool is a thin call to the API; the daemon enforces every rule. Answers are short text,
because every word costs context. Errors read "Waypoint unavailable" or the API's own message,
never a traceback, a path or a token.
"""

from __future__ import annotations

import functools
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .client import ApiFailure, Unavailable, WaypointClient, read_token
from .present import InputError, answer_line, flat, item_id, message_line, recurrence_id, recurrence_line, to_schedule, line, local, to_answer_at, to_condition, to_ts, to_when
from .schema import limits

INSTRUCTIONS = ("Waypoint keeps your open items, what waits on what, and wakes you at the right time. "
                "Call wp_mine after every restart or compaction; update an item at every state change.")


# ---------------------------------------------------------------------- parameter limits
# From the contract (schema.limits()), so they can't drift from what the daemon accepts.
_L = limits()
Title = Annotated[str, Field(description=f"one line, at most {_L['title']} characters")]
TitleOpt = Annotated[str | None, Field(description=f"one line, at most {_L['title']} characters")]
Label = Annotated[str | None, Field(description=f"short form for tight places, at most {_L['label']} characters")]
Note = Annotated[str | None, Field(description=f"one line of status, at most {_L['note']} characters")]
NoteReq = Annotated[str, Field(description=f"closing note, at most {_L['note']} characters")]
ScheduleText = Annotated[str | None, Field(description="'every 15m', 'daily 09:00', 'weekly mon,thu 06:40' or "
                                                       "'monthly 1 04:30'; optional 'tz Europe/Paris' at the end")]
QuestionText = Annotated[str, Field(description=f"at most {_L['question']} characters")]
Options = Annotated[list[str] | None,
                    Field(description=f"at most {_L['options']} options of at most {_L['option']} characters")]
Recommendation = Annotated[str | None, Field(description=f"at most {_L['recommendation']} characters")]
Links = Annotated[list[str] | None, Field(description=f"at most {_L['links']} http(s) URLs")]
RunningUntil = Annotated[str | None, Field(description="start it running until this time (default: Waypoint's "
                                                           "lease, renewed by your updates)")]
RunningUntilUpd = Annotated[str | None, Field(description="while running: until when (any update of yours renews "
                                                              "the default lease anyway)")]
AnswerAt = Annotated[str | None, Field(description="where the person answers: an issue or PR URL, or a session's "
                                                   "client name; required if links hold an issue")]


# ---------------------------------------------------------------------- the server

def build(client: WaypointClient, me: str) -> MCPServer:
    server = MCPServer("waypoint", instructions=INSTRUCTIONS)

    def api(method: str, path: str, body=None, query=None):
        try:
            return flat(client.request(method, path, body, query))
        except Unavailable:
            raise ToolError("Waypoint unavailable") from None
        except ApiFailure as e:
            where = f" ({e.field})" if e.field else ""
            cycle = f": {' → '.join(e.cycle)}" if e.cycle else ""
            raise ToolError(f"{e.code}{where}: {e.message}{cycle}") from None

    def tool(**kw):
        """server.tool, with bad input reported as a tool error."""
        register = server.tool(**kw)

        def wrap(fn):
            @functools.wraps(fn)
            def inner(*a, **k):
                try:
                    return fn(*a, **k)
                except InputError as e:
                    raise ToolError(str(e)) from None
            return register(inner)
        return wrap

    def get(iid: str) -> dict:
        return api("GET", f"/v1/items/{item_id(iid)}")

    @tool(description="My open items, what waits on them, and what is due in the next 24 hours. "
                             "Call after every restart or compaction.", structured_output=False)
    def wp_mine() -> str:
        items = api("GET", "/v1/items", query={"owner": me})["items"]
        out = [f"Open items of {me}:"] if items else [f"No open items for {me}."]
        horizon = datetime.now(timezone.utc) + timedelta(hours=24)
        soon = []
        for it in items:
            out.append(line(it))
            waiting = [get(w) for w in it["waited_on_by"]]
            waiting = [w for w in waiting if w["state"] in ("next", "running", "waiting")]
            if waiting:
                out.append("  waited on by: " + "; ".join(f"{w['id']} {w['title']} ({w['owner']})" for w in waiting))
            for key in ("due_at", "wake_at"):
                t = it.get(key)
                if t and datetime.fromisoformat(t.replace("Z", "+00:00")) <= horizon:
                    soon.append((t, f"{local(t)} {key.split('_')[0]} {it['id']} {it['title']}"))
        if soon:
            out.append("Next 24 hours:")
            out.extend("  " + s for _, s in sorted(soon))
        try:
            recs = api("GET", "/v1/recurrences", query={"owner": me})["recurrences"]
        except ToolError as e:  # a Waypoint from before recurrences (e.g. during an upgrade): none
            if not str(e).startswith("not_found"):
                raise
            recs = []
        if recs:
            out.append("Recurring:")
            out.extend("  " + recurrence_line(r) for r in recs)
        return "\n".join(out)

    @tool(description="List items. Filters: owner, track, state (comma list or 'all'), waiting_on "
                             "(items waiting on that owner).", structured_output=False)
    def wp_list(owner: str | None = None, track: str | None = None, state: str | None = None,
                waiting_on: str | None = None) -> str:
        got = api("GET", "/v1/items", query={"owner": owner, "track": track, "state": state,
                                             "waiting_on": waiting_on})
        lines = [line(it) for it in got["items"]] or ["No items."]
        if got["truncated"]:
            lines.append("(more; narrow the filter)")
        return "\n".join(lines)

    @tool(description="One item in full, with what waits on it and its recent history.",
                 structured_output=False)
    def wp_get(id: str) -> str:
        if id.strip().upper().startswith("R-"):
            r = api("GET", f"/v1/recurrences/{recurrence_id(id)}")
            out = [recurrence_line(r)]
            for k in ("label", "note"):
                if r.get(k):
                    out.append(f"{k}: {r[k]}")
            out.append(f"window {r['window']} · overlap {r['overlap']} · catch-up {r['catch_up']}")
            return "\n".join(out)
        it = get(id)
        out = [line(it)]
        if it.get("label"):
            out.append(f"label: {it['label']}")
        if it.get("question"):
            q = it["question"]
            out.append(f"question: {q['text']}")
            if q.get("options"):
                out.append("options: " + " | ".join(q["options"]))
            if q.get("recommendation"):
                out.append(f"recommendation: {q['recommendation']}")
        if answer_line(it):
            out.append(answer_line(it))
        if it["waited_on_by"]:
            out.append("waited on by: " + ", ".join(it["waited_on_by"]))
        if it["at_risk_from"]:
            out.append("at risk from: " + ", ".join(f"{s['item']} ({s['why']})" for s in it["at_risk_from"]))
        for link in it["links"]:
            out.append(f"link: {link['url']}" + (f" ({link['label']})" if link.get("label") else ""))
        out.append("history:")
        for h in it["history"][-8:]:
            what = h["note"] or (", ".join(h["changes"]) if h["changes"] else "")
            out.append(f"  {local(h['at'])} {h['by'] or 'waypoint'} {h['event']}" + (f": {what}" if what else ""))
        return "\n".join(out)

    @tool(description="Add an item. kind: task|decision|approval|event. waits_on: item ids (W-12) or times. "
                             "when: a day (2026-03-12) or a time; due/wake: times (local if no zone).",
                 structured_output=False)
    def wp_add(title: Title, track: str, kind: str = "task", owner: str | None = None, label: Label = None,
               waits_on: list[str] | None = None, due: str | None = None, wake: str | None = None,
               when: str | None = None, until: str | None = None, note: Note = None,
               links: Links = None, running_until: RunningUntil = None) -> str:
        body: dict = {"title": title, "track": track, "kind": kind}
        if running_until:
            body.update(state="running", running_until=to_ts(running_until))
        if owner:
            body["owner"] = owner
        if label:
            body["label"] = label
        if waits_on:
            body["waits_on"] = [to_condition(w) for w in waits_on]
        if due:
            body["due_at"] = to_ts(due)
        if wake:
            body["wake_at"] = to_ts(wake)
        if when:
            body["when"] = to_when(when, until)
        if note:
            body["note"] = note
        if links:
            body["links"] = [{"url": u} for u in links]
        return "Added " + line(api("POST", "/v1/items", body))

    @tool(description="Change one of my items. state: running|next|done|dropped; '' clears a field. "
                             "add_waits_on adds conditions; waits_on replaces them.", structured_output=False)
    def wp_update(id: str, state: str | None = None, note: Note = None, due: str | None = None,
                  wake: str | None = None, when: str | None = None, until: str | None = None,
                  add_waits_on: list[str] | None = None, waits_on: list[str] | None = None,
                  title: TitleOpt = None, label: Label = None, owner: str | None = None,
                  answer_at: AnswerAt = None, running_until: RunningUntilUpd = None) -> str:
        body: dict = {}
        if running_until:
            body["running_until"] = to_ts(running_until)
        if answer_at is not None:
            body["answer_at"] = to_answer_at(answer_at)
        for key, val in (("state", state), ("title", title), ("owner", owner)):
            if val:
                body[key] = val
        for key, val in (("note", note), ("label", label)):
            if val is not None:
                body[key] = val or None
        if due is not None:
            body["due_at"] = to_ts(due) if due else None
        if wake is not None:
            body["wake_at"] = to_ts(wake) if wake else None
        if when is not None:
            body["when"] = to_when(when, until) if when else None
        if add_waits_on:
            body["add_waits_on"] = [to_condition(w) for w in add_waits_on]
        if waits_on is not None:
            body["waits_on"] = [to_condition(w) for w in waits_on]
        if not body:
            raise ToolError("nothing to change")
        return "Updated " + line(api("PATCH", f"/v1/items/{item_id(id)}", body))

    @tool(description="Mark my item done with a closing note; Waypoint wakes whoever waited on it. For a person's "
                      'question I asked: quote the answer ("…") and say where it was given.',
                 structured_output=False)
    def wp_done(id: str, note: NoteReq) -> str:
        it = api("PATCH", f"/v1/items/{item_id(id)}", {"state": "done", "note": note})
        waiting = [get(w) for w in it["waited_on_by"]]
        free = [w["id"] for w in waiting if w["state"] == "next"]
        still = [w["id"] for w in waiting if w["state"] == "waiting"]
        text = f"Done {it['id']} {it['title']}."
        if free:
            text += f" Now free to start (owners told): {', '.join(free)}."
        if still:
            text += f" Still waiting on something else: {', '.join(still)}."
        return text

    @tool(description="Put a decision to a human: creates their item with the question, options and your "
                             "recommendation, and makes my item (for) wait on it.", structured_output=False)
    def wp_ask(human: str, question: QuestionText, title: Title, options: Options = None,
               recommendation: Recommendation = None, for_item: str | None = None, track: str | None = None,
               due: str | None = None, answer_at: AnswerAt = None, links: Links = None) -> str:
        q = {"text": question, "options": options or [], "recommendation": recommendation}
        extra: dict = {}
        if answer_at:
            extra["answer_at"] = to_answer_at(answer_at)
        if links:
            extra["links"] = [{"url": u} for u in links]
        if for_item:
            body = {"human": human, "title": title, "question": q, **extra}
            if track:
                body["track"] = track
            if due:
                body["due_at"] = to_ts(due)
            got = api("POST", f"/v1/items/{item_id(for_item)}/ask", body)  # one step on the server
            return f"Asked {human}: {got['asked']['id']} {got['asked']['title']}. {got['item']['id']} waits on it."
        if not track:
            raise ToolError("give for_item (the item that waits on the answer) or a track")
        body = {"owner": human, "track": track, "title": title, "kind": "decision", "question": q, **extra}
        if due:
            body["due_at"] = to_ts(due)
        asked = api("POST", "/v1/items", body)
        return f"Asked {human}: {asked['id']} {asked['title']}."

    @tool(description="Create (no id: title, track, schedule) or change a recurrence, a schedule that gives its "
                      "owner an item each time. state: active|paused|stopped (final).", structured_output=False)
    def wp_recur(id: str | None = None, title: TitleOpt = None, track: str | None = None,
                 schedule: ScheduleText = None, owner: str | None = None, label: Label = None, note: Note = None,
                 window: str | None = None, overlap: str | None = None, catch_up: str | None = None,
                 checkin: bool | None = None, checkin_by: str | None = None, state: str | None = None) -> str:
        body = {k: v for k, v in (("title", title), ("track", track), ("owner", owner), ("label", label),
                                  ("note", note), ("window", window), ("overlap", overlap), ("catch_up", catch_up),
                                  ("checkin", checkin), ("checkin_by", checkin_by), ("state", state)) if v is not None}
        if schedule:
            body["schedule"] = to_schedule(schedule)
        if id:
            r = api("PATCH", f"/v1/recurrences/{recurrence_id(id)}", body)
        else:
            body.pop("state", None)
            r = api("POST", "/v1/recurrences", body)
        return recurrence_line(r)

    @tool(description="My recent messages from Waypoint in full, newest first: what a short wake-up line only "
                      "names (an outage summary, an undeliverable message), with what became of each.",
          structured_output=False)
    def wp_messages(limit: int = 10, since: str | None = None) -> str:
        query = {"limit": max(1, min(int(limit), 100))}
        if since:
            query["since"] = to_ts(since)
        msgs = api("GET", "/v1/messages", query=query)["messages"]
        if not msgs:
            return f"No messages for {me}."
        return "\n".join([f"Messages for {me}, newest first:"] + [message_line(m) for m in msgs])

    return server


def main() -> int:
    me = os.environ.get("WAYPOINT_NAME", "").strip()
    if not me:
        print("waypoint-mcp: set WAYPOINT_NAME to this session's client name", file=sys.stderr)
        return 2
    url = os.environ.get("WAYPOINT_URL", "http://127.0.0.1:7130")
    token_file = os.environ.get("WAYPOINT_TOKEN_FILE") or os.path.expanduser(f"~/.config/waypoint/tokens/{me}.token")
    try:
        token = read_token(token_file)
    except (OSError, ValueError):
        print("waypoint-mcp: cannot read the token file (WAYPOINT_TOKEN_FILE)", file=sys.stderr)
        return 2
    build(WaypointClient(url, token, timeout=20), me).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
