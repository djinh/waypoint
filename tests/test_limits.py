"""The length limits are stated where people and agents meet them: in refusals, in the MCP
tools' parameter descriptions and in `wp --help`, all taken from the contract."""

from __future__ import annotations

import asyncio
import io

import pytest
from mcp import Client

from waypoint import schema, wp
from waypoint.client import WaypointClient
from waypoint.errors import ApiError
from waypoint.mcp_server import build

L = schema.limits()


def test_limits_come_from_the_contract():
    assert L == {"title": 80, "label": 24, "note": 200, "question": 500, "option": 80, "options": 8,
                 "recommendation": 200, "url": 500, "links": 10, "waits_on": 20}


@pytest.mark.parametrize("name,body,message", [
    ("ItemPatch", {"note": "x" * 260}, "note: 260 characters, at most 200"),
    ("ItemPatch", {"label": "x" * 30}, "label: 30 characters, at most 24"),
    ("ItemPatch", {"title": "x" * 81}, "title: 81 characters, at most 80"),
    ("ItemPatch", {"title": ""}, "title: 0 characters, at least 1"),
    ("ItemPatch", {"question": {"text": "q", "options": ["o"] * 9}}, "question.options: 9 entries, at most 8"),
    ("ItemPatch", {"question": {"text": "q", "options": ["o" * 81]}}, "question.options.0: 81 characters, at most 80"),
    ("ItemPatch", {"question": {"text": "q", "recommendation": "r" * 201}},
     "question.recommendation: 201 characters, at most 200"),
    ("ItemPatch", {"state": "late"}, "state: one of running, done, dropped, next, waiting"),
    ("ItemCreate", {"track": "t", "kind": "task", "title": "t", "links": [{"url": "https://a.example.org/"}] * 11},
     "links: 11 entries, at most 10"),
])
def test_refusals_state_the_limit(name, body, message):
    with pytest.raises(ApiError) as e:
        schema.check(name, body)
    assert e.value.message == message
    assert "xxxx" not in e.value.message  # the value itself is never repeated


def test_refusal_over_http(server, tokens, mk):
    it = mk("agent-a", "Something")
    status, out, _ = server("PATCH", f"/v1/items/{it['id']}", tokens["agent-a"], {"note": "n" * 260})
    assert status == 400 and out["error"] == {"code": "invalid", "message": "note: 260 characters, at most 200",
                                              "field": "note"}


def test_mcp_parameters_state_the_limits():
    async def run():
        async with Client(build(WaypointClient("http://127.0.0.1:9", "x"), "agent-a")) as c:
            return {t.name: t.input_schema["properties"] for t in (await c.list_tools()).tools}

    tools = asyncio.run(run())
    d = lambda tool, param: tools[tool][param]["description"]  # noqa: E731
    assert f"at most {L['title']} characters" in d("wp_add", "title")
    assert f"at most {L['label']} characters" in d("wp_add", "label")
    assert f"at most {L['note']} characters" in d("wp_add", "note")
    assert f"at most {L['note']} characters" in d("wp_update", "note")
    assert f"at most {L['title']} characters" in d("wp_update", "title")
    assert f"at most {L['note']} characters" in d("wp_done", "note")
    assert f"at most {L['question']} characters" in d("wp_ask", "question")
    assert f"at most {L['options']} options of at most {L['option']} characters" in d("wp_ask", "options")
    assert f"at most {L['recommendation']} characters" in d("wp_ask", "recommendation")


def test_wp_help_states_the_limits(capsys):
    with pytest.raises(SystemExit):
        wp.main(["--help"])
    out = capsys.readouterr().out
    assert f"title {L['title']} characters, label {L['label']}, note {L['note']}" in " ".join(out.split())
    with pytest.raises(SystemExit):
        wp.main(["add", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert f"at most {L['title']} characters" in out and f"at most {L['note']} characters" in out


def test_wp_refusal_states_the_limit(server, tokens, tracks, tmp_path):
    tf = tmp_path / "t"
    tf.write_text(tokens["agent-a"])
    err = io.StringIO()
    code = wp.main(["--url", server.base, "--token-file", str(tf), "add", "x" * 90, "--track", "release"],
                   io.StringIO(), err)
    assert code == 1 and err.getvalue().strip() == "wp: invalid (title): title: 90 characters, at most 80"
