"""The HTTP API over a real server on 127.0.0.1: tokens, status codes, and every response in the contract's shape."""

from __future__ import annotations

import pytest

from waypoint.schema import validator


def valid(name: str, body) -> None:
    errors = list(validator(name).iter_errors(body))
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors[:3]]


ROUTES = [
    ("GET", "/v1/items"),
    ("POST", "/v1/items"),
    ("GET", "/v1/items/W-1"),
    ("PATCH", "/v1/items/W-1"),
    ("GET", "/v1/tracks"),
    ("PUT", "/v1/tracks/release"),
    ("GET", "/v1/views/timeline"),
    ("GET", "/v1/views/for/alice"),
    ("GET", "/v1/no-such-endpoint"),
    ("GET", "/"),
]


def test_health_needs_no_token(server):
    status, body, _ = server("GET", "/v1/health")
    assert status == 200
    valid("Health", body)


@pytest.mark.parametrize("method,path", ROUTES)
def test_no_token_no_data(server, tokens, method, path):
    for headers in ({}, {"Authorization": "Bearer not-a-token"}, {"Authorization": f"Basic {tokens['alice']}"}):
        status, body, _ = server(method, path, body={} if method in ("POST", "PATCH", "PUT") else None,
                                 headers=headers)
        assert status == 401, (method, path, headers)
        assert body == {"error": {"code": "unauthorized", "message": "a valid token is needed"}}


def test_token_in_query_string_is_ignored(server, tokens):
    status, _, _ = server("GET", f"/v1/items?token={tokens['alice']}")
    assert status == 401


def test_retired_client_is_refused(server, store, tokens):
    store.retire_client("agent-c")
    assert server("GET", "/v1/items", tokens["agent-c"])[0] == 401


def test_item_round_trip(server, tokens, tracks):
    status, item, headers = server("POST", "/v1/items", tokens["agent-a"], {
        "track": "release", "title": "Production release of 2.4", "label": "production release",
        "kind": "task", "due_at": "2026-03-12T18:00:00+01:00",
        "links": [{"url": "https://git.example.org/acme/app/issues/12", "label": "#12"}]})
    assert status == 201, item
    valid("Item", item)
    assert headers["location"] == f"/v1/items/{item['id']}"
    assert item["due_at"] == "2026-03-12T17:00:00Z"
    status, detail, _ = server("GET", f"/v1/items/{item['id']}", tokens["pages"])
    assert status == 200
    valid("ItemDetail", detail)
    assert detail["history"][0]["event"] == "created"
    status, patched, _ = server("PATCH", f"/v1/items/{item['id']}", tokens["agent-a"],
                                {"state": "running", "note": "deploying", "if_version": 1})
    assert status == 200 and patched["state"] == "running" and patched["version"] == 2
    valid("Item", patched)
    status, listed, _ = server("GET", "/v1/items?owner=agent-a&state=running", tokens["pages"])
    assert status == 200 and [i["id"] for i in listed["items"]] == [item["id"]] and not listed["truncated"]


def test_errors_are_in_the_contracts_shape(server, tokens, tracks):
    secret_value = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    cases = [
        ("POST", "/v1/items", {"track": "release", "title": "x", "kind": "task", "colour": secret_value}, 400, "invalid", "colour"),
        ("POST", "/v1/items", {"track": "release", "title": "x" * 81, "kind": "task"}, 400, "invalid", "title"),
        ("POST", "/v1/items", {"track": "release", "kind": "task"}, 400, "invalid", "title"),
        ("POST", "/v1/items", {"track": "release", "title": "x", "kind": "task", "due_at": "2026-03-12T17:00:00"}, 400, "invalid", "due_at"),
        ("POST", "/v1/items", {"track": "release", "title": secret_value, "kind": "task"}, 422, "content", "title"),
        ("POST", "/v1/items", {"track": "nope", "title": "x", "kind": "task"}, 400, "invalid", "track"),
        ("GET", "/v1/items/W-999", None, 404, "not_found", None),
        ("GET", "/v1/items/nonsense", None, 404, "not_found", None),
        ("GET", "/v1/items?colour=red", None, 400, "invalid", "colour"),
        ("GET", "/v1/items?state=late", None, 400, "invalid", "state"),
        ("GET", "/v1/items?limit=0", None, 400, "invalid", "limit"),
        ("GET", "/v1/items?changed_since=yesterday", None, 400, "invalid", "changed_since"),
        ("GET", "/v1/views/for/agent-a", None, 404, "not_found", None),
        ("GET", "/v1/views/for/nobody", None, 404, "not_found", None),
        ("GET", "/v1/views/timeline?from=2026-03-10T00:00:00Z&to=2026-03-09T00:00:00Z", None, 400, "invalid", "to"),
        ("DELETE", "/v1/items/W-1", None, 405, "method_not_allowed", None),
        ("GET", "/v1/elsewhere", None, 404, "not_found", None),
    ]
    for method, path, body, status, code, field in cases:
        got, out, _ = server(method, path, tokens["agent-a"], body)
        assert (got, out["error"]["code"]) == (status, code), (method, path, out)
        assert out["error"].get("field") == field, (path, out)
        valid("Error", out)
        assert secret_value not in str(out)


def test_malformed_bodies(server, tokens, tracks):
    for raw in (b"not json", b"[1, 2]", b"\xff\xfe"):
        status, out, _ = server("POST", "/v1/items", tokens["agent-a"], raw=raw)
        assert status == 400 and out["error"]["code"] == "invalid"
    big = b'{"note": "' + b"x" * 70_000 + b'"}'
    status, out, _ = server("POST", "/v1/items", tokens["agent-a"], raw=big)
    assert status == 400 and out["error"]["message"] == "body: too large"


def test_roles_over_http(server, tokens, tracks, mk):
    it = mk("agent-a", "Someone's item")
    assert server("GET", "/v1/items", tokens["pages"])[0] == 200
    assert server("POST", "/v1/items", tokens["pages"], {"track": "release", "title": "x", "kind": "task"})[0] == 403
    assert server("PATCH", f"/v1/items/{it['id']}", tokens["agent-b"], {"note": "x"})[0] == 403
    for path in ("/v1/items", f"/v1/items/{it['id']}", "/v1/tracks", "/v1/views/timeline", "/v1/views/for/alice"):
        status, out, _ = server("GET", path, tokens["host-1"])
        assert status == 403 and out["error"]["code"] == "forbidden", path


def test_loop_and_version_conflict(server, tokens, mk):
    a = mk("agent-a", "A")
    b = mk("agent-a", "B", waits_on=[{"item": a["id"]}])
    status, out, _ = server("PATCH", f"/v1/items/{a['id']}", tokens["agent-a"], {"waits_on": [{"item": b["id"]}]})
    assert status == 409 and out["error"]["code"] == "loop" and out["error"]["cycle"] == [a["id"], b["id"], a["id"]]
    valid("Error", out)
    status, out, _ = server("PATCH", f"/v1/items/{a['id']}", tokens["agent-a"], {"note": "x", "if_version": 7})
    assert status == 409 and out["error"]["code"] == "version_conflict"


def test_tracks_over_http(server, tokens):
    status, t, _ = server("PUT", "/v1/tracks/release", tokens["agent-a"],
                          {"title": "Release 2.4", "lead": "agent-a", "order": 10})
    assert status == 200
    valid("Track", t)
    status, out, _ = server("PUT", "/v1/tracks/release", tokens["agent-b"], {"title": "Mine"})
    assert status == 403
    status, out, _ = server("PUT", "/v1/tracks/Bad_Id", tokens["agent-a"], {"title": "x"})
    assert status == 400
    status, out, _ = server("GET", "/v1/tracks", tokens["pages"])
    assert status == 200 and [x["id"] for x in out["tracks"]] == ["release"]


def test_views_over_http(server, tokens, mk):
    q = mk("alice", "Choose the strategy", caller="agent-b", kind="decision", question={"text": "Which?"})
    mk("agent-b", "Migration tool", track="infra", waits_on=[{"item": q["id"]}])
    status, tl, _ = server("GET", "/v1/views/timeline", tokens["pages"])
    assert status == 200
    valid("TimelineView", tl)
    assert tl["tz"] == "Europe/Paris" and len(tl["items"]) == 2 and len(tl["edges"]) == 1
    status, fv, _ = server("GET", "/v1/views/for/alice", tokens["pages"])
    assert status == 200
    valid("ForView", fv)
    assert fv["entries"][0]["id"] == q["id"] and fv["entries"][0]["unblocks"][0]["depth"] == 1
    status, tl, _ = server("GET", "/v1/views/timeline?track=infra", tokens["pages"])
    assert status == 200
    # the blocker from the other track comes along, with its track
    assert {i["id"] for i in tl["items"]} == {q["id"], "W-2"}
    assert {t["id"] for t in tl["tracks"]} == {"infra", "release"}


def test_internal_errors_log_the_type_only(server, store, tokens, capfd, caplog):
    """An unexpected error answers 500 in the contract's shape; its message never reaches a log."""

    def broken(*a, **kw):
        raise ValueError("value from a request: hunter2")

    store.tracks = broken
    status, out, _ = server("GET", "/v1/tracks", tokens["alice"])
    assert status == 500 and out == {"error": {"code": "unavailable", "message": "internal error"}}
    # the server logs after the answer is sent: wait for Waypoint's own line, then look at every log
    import time
    deadline = time.monotonic() + 5
    while "internal error on GET /v1/tracks: ValueError" not in caplog.text and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)  # room for anything else the server would log after it
    assert "internal error on GET /v1/tracks: ValueError" in caplog.text
    err = capfd.readouterr().err
    assert "hunter2" not in caplog.text + err
    assert "Traceback" not in caplog.text + err
