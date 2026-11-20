"""POST /v1/items/{id}/ask (one step, no orphans, no repeats) and /v1/clients (admins only)."""

from __future__ import annotations

import pytest

from waypoint.errors import ApiError
from waypoint.schema import validator


def valid(name, body):
    errors = list(validator(name).iter_errors(body))
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors[:3]]


ASK = {"human": "alice", "title": "Choose the migration strategy",
       "question": {"text": "Online or offline?", "options": ["online", "offline"], "recommendation": "online"}}


def test_ask_creates_and_waits_in_one_step(store, mk, who):
    tool = mk("agent-b", "Migration tool", track="infra")
    asked, item, created = store.ask(who("agent-b"), tool["id"], ASK)
    assert created and asked["owner"] == "alice" and asked["kind"] == "decision" and asked["track"] == "infra"
    assert asked["created_by"] == "agent-b"
    assert item["state"] == "waiting" and item["waits_on"][0]["item"] == asked["id"]


def test_ask_again_returns_the_open_question(store, mk, who):
    tool = mk("agent-b", "Migration tool", track="infra")
    first, _, _ = store.ask(who("agent-b"), tool["id"], ASK)
    again, item, created = store.ask(who("agent-b"), tool["id"], ASK)
    assert not created and again["id"] == first["id"] and len(item["waits_on"]) == 1
    store.patch_item(who("alice"), first["id"], {"state": "done", "note": "online"})
    third, _, created = store.ask(who("agent-b"), tool["id"], ASK)  # answered: a new question is new
    assert created and third["id"] != first["id"]


@pytest.mark.parametrize("caller,body,code", [
    ("agent-a", ASK, "forbidden"),                               # not the asker's item
    ("agent-b", dict(ASK, human="agent-c"), "invalid"),          # not a human
    ("agent-b", dict(ASK, track="nope"), "invalid"),
    ("agent-b", dict(ASK, title="x" * 81), "invalid"),
    ("agent-b", dict(ASK, question={"text": "mail me at a@example.org"}), "content"),
    ("pages", ASK, "forbidden"),
])
def test_a_refused_ask_leaves_nothing(store, mk, who, caller, body, code):
    tool = mk("agent-b", "Migration tool", track="infra")
    with pytest.raises(ApiError) as e:
        store.ask(who(caller), tool["id"], body)
    assert e.value.code == code
    assert [i["id"] for i in store.items()] == [tool["id"]]
    assert store.get_item(tool["id"])["waits_on"] == []


def test_ask_over_http(server, tokens, mk):
    mk("agent-b", "Migration tool", track="infra")
    status, out, _ = server("POST", "/v1/items/W-1/ask", tokens["agent-b"], ASK)
    assert status == 201
    valid("AskResult", out)
    status, out, _ = server("POST", "/v1/items/W-1/ask", tokens["agent-b"], ASK)
    assert status == 200 and out["asked"]["id"] == "W-2"
    assert server("POST", "/v1/items/W-1/ask", tokens["agent-a"], ASK)[0] == 403


# ---------------------------------------------------------------------- /v1/clients


@pytest.mark.parametrize("name", ["agent-a", "pages", "host-1"])
def test_clients_are_for_admins_only(server, tokens, name):
    for method, path, body in (("GET", "/v1/clients", None),
                               ("POST", "/v1/clients", {"name": "x", "role": "agent"}),
                               ("PATCH", "/v1/clients/agent-b", {"label": "x"}),
                               ("POST", "/v1/clients/agent-b/rotate", None)):
        status, out, _ = server(method, path, tokens[name], body)
        assert status == 403, (name, method, path)


@pytest.mark.parametrize("admin", ["coordinator", "alice"])
def test_register_rotate_retire(server, store, tokens, admin):
    tok = tokens[admin]
    status, out, _ = server("POST", "/v1/clients", tok, {"name": "agent-d", "role": "agent", "label": "Docs",
                                                          "deliverer": "host-1"})
    assert status == 201
    valid("ClientWithToken", out)
    new = out["token"]
    assert out["client"]["deliverer"] == "host-1"
    assert server("GET", "/v1/items", new)[0] == 200
    status, listed, _ = server("GET", "/v1/clients", tok)
    valid("Client", listed["clients"][0])
    assert "token" not in str(listed) and new not in str(listed)
    assert server("POST", "/v1/clients", tok, {"name": "agent-d", "role": "agent"})[0] == 400  # taken
    status, out, _ = server("POST", "/v1/clients/agent-d/rotate", tok)
    assert status == 200 and out["token"] != new
    assert server("GET", "/v1/items", new)[0] == 401  # the old token stops at once
    assert server("GET", "/v1/items", out["token"])[0] == 200
    status, out, _ = server("PATCH", "/v1/clients/agent-d", tok, {"retired": True})
    assert status == 200 and out["retired_at"]
    assert server("PATCH", "/v1/clients/agent-d", tok, {"label": "back"})[0] == 404


def test_retiring_needs_the_items_handed_over(server, store, tokens, mk, who):
    it = mk("agent-c", "Docs search")
    status, out, _ = server("PATCH", "/v1/clients/agent-c", tokens["coordinator"], {"retired": True})
    assert status == 400 and out["error"]["field"] == "retired"
    store.patch_item(who("coordinator"), it["id"], {"owner": "agent-b"})
    status, out, _ = server("PATCH", "/v1/clients/agent-c", tokens["coordinator"], {"retired": True})
    assert status == 200
    assert server("GET", "/v1/items", tokens["agent-c"])[0] == 401


def test_retiring_a_deliverer_needs_its_recipients_moved(server, store, tokens):
    store.route("agent-a", "host-1")
    status, out, _ = server("PATCH", "/v1/clients/host-1", tokens["alice"], {"retired": True})
    assert status == 400 and out["error"]["field"] == "retired"


def test_client_input_checks(server, tokens):
    tok = tokens["coordinator"]
    assert server("POST", "/v1/clients", tok, {"name": "Bad Name", "role": "agent"})[0] == 400
    assert server("POST", "/v1/clients", tok, {"name": "x", "role": "admin"})[0] == 400
    assert server("POST", "/v1/clients", tok, {"name": "x", "role": "agent", "deliverer": "agent-a"})[0] == 400
    assert server("POST", "/v1/clients", tok, {"name": "x", "role": "agent", "label": "192.0.2.1"})[0] == 422
    assert server("PATCH", "/v1/clients/nobody", tok, {"label": "x"})[0] == 404
    status, out, _ = server("PATCH", "/v1/clients/agent-a", tok, {"deliverer": "host-1"})
    assert status == 200 and out["deliverer"] == "host-1"


# A coordinator manages agents, deliverers and readers; human and coordinator clients only a human
# (design owner, 2026-11-14).
COORD_REFUSED = [
    ("POST", "/v1/clients", {"name": "bob2", "role": "human"}),
    ("POST", "/v1/clients", {"name": "coord2", "role": "coordinator"}),
    ("POST", "/v1/clients/alice/rotate", None),
    ("POST", "/v1/clients/coordinator/rotate", None),
    ("PATCH", "/v1/clients/bob", {"retired": True}),
    ("PATCH", "/v1/clients/coordinator", {"retired": True}),
    ("PATCH", "/v1/clients/alice", {"deliverer": "host-1"}),
    ("PATCH", "/v1/clients/coordinator", {"deliverer": "host-1"}),
    ("PATCH", "/v1/clients/alice", {"label": "Someone else"}),
    ("PATCH", "/v1/clients/coordinator", {"label": "Boss"}),
]


@pytest.mark.parametrize("method,path,body", COORD_REFUSED)
def test_a_coordinator_cannot_touch_humans_or_coordinators(server, store, tokens, method, path, body):
    before = {c["name"]: c for c in store.list_clients(store.authenticate(tokens["alice"]))}
    status, out, _ = server(method, path, tokens["coordinator"], body)
    assert status == 403 and out["error"]["code"] == "forbidden"
    after = {c["name"]: c for c in store.list_clients(store.authenticate(tokens["alice"]))}
    assert after == before
    assert server("GET", "/v1/items", tokens["alice"])[0] == 200  # alice's token still works


@pytest.mark.parametrize("method,path,body,status", [
    ("POST", "/v1/clients", {"name": "carol", "role": "human"}, 201),
    ("POST", "/v1/clients", {"name": "coord2", "role": "coordinator"}, 201),
    ("POST", "/v1/clients/coordinator/rotate", None, 200),
    ("POST", "/v1/clients/bob/rotate", None, 200),
    ("PATCH", "/v1/clients/coordinator", {"label": "Coordinator", "deliverer": "host-1"}, 200),
    ("PATCH", "/v1/clients/bob", {"retired": True}, 200),
])
def test_a_human_can(server, tokens, method, path, body, status):
    assert server(method, path, tokens["alice"], body)[0] == status


def test_a_coordinator_manages_agents_deliverers_readers(server, tokens):
    tok = tokens["coordinator"]
    for role in ("agent", "deliverer", "reader"):
        assert server("POST", "/v1/clients", tok, {"name": f"new-{role}", "role": role})[0] == 201
    assert server("POST", "/v1/clients/agent-c/rotate", tok)[0] == 200
    assert server("PATCH", "/v1/clients/agent-c", tok, {"label": "Docs", "deliverer": "host-1"})[0] == 200
    assert server("PATCH", "/v1/clients/new-agent", tok, {"retired": True})[0] == 200
