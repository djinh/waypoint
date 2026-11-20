"""The content check: what must not be stored is refused, ordinary status text is not."""

from __future__ import annotations

import pytest

from waypoint import content
from waypoint.errors import ApiError

REFUSED = [
    "hash " + "a3f5" * 10,
    "token ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2",
    "github_pat_" + "11ABCDEFG0123456789_abcdefghij",
    "key sk-" + "proj-abcdefghijklmnop1234",
    "xoxb-" + "1234567890-abcdefghij",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "Authorization: Bearer " + "abcdefghijklmnop1234",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "see https://files.example.org/a.zip?X-Amz-Signature=abc",
    "https://example.org/hook?token=abc",
    "https://user:secret@example.org/",
    "mail me at someone@example.org",
    "server at 192.0.2.10",
    "net 198.51.100.0/24",
    "v6 2001:db8::1 is up",
    "fe80::1ff:fe23:4567:890a",
    "random Zq8x7Vb2Nk4Lm9Pw3Rt6Ys1Ud5Fg0Hj2K",
    "key " + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5bC7d",  # 40 random characters
]

LINKS_ALLOWED = [
    "https://github.com/anthropics/claude-code/issues/12345",
    "https://git.example.org/acme/some-long-repository-name/issues/1234",
    "https://docs.example.org/plans/2026-11-14-migration-plan-v2",
    "https://git.example.org/acme/app/commit/0123456789abcdef0123456789abcdef01234567",
    "https://git.example.org/acme/app/blob/0123456789abcdef0123456789abcdef01234567/src/main.py",
    "https://git.example.org/acme/app/tree/0123456789abcdef0123456789abcdef01234567",
    "https://git.example.org/acme/app/pull/12/commits/0123456789abcdef0123456789abcdef01234567",
    "https://git.example.org/acme/app/commit/" + "0123456789abcdef" * 4,  # SHA-256
]

LINKS_REFUSED = [
    "https://hooks.example.org/services/0123456789abcdef0123456789abcdef01234567",
    "https://ping.example.org/0123456789abcdef0123456789abcdef",
    "https://git.example.org/acme/app/commit/" + "0123456789abcdef" * 3,  # 48: not a git id
    "https://git.example.org/acme/app/raw/0123456789abcdef0123456789abcdef01234567/x",
    "https://files.example.org/0123456789abcdef0123456789abcdef01234567/a.zip?X-Amz-Signature=abc",
    "https://example.org/x?state=0123456789abcdef0123456789abcdef01234567",
    "https://user:secret@example.org/0123456789abcdef",
    "https://192.0.2.10/acme/app/commit/0123456789abcdef0123456789abcdef01234567",
]

ALLOWED = [
    "build 102 out, waiting on 12 testers' feedback",
    "released 14:02, soak 13:10–16:40",
    "check at 12:00:00 sharp",
    "release 2.4 and hotfix 2.3.5",
    "gate-2026.11.13-2 soaking",
    "https://git.example.org/acme/app/pull/88",
    "https://docs.example.org/plans/migration-2-4",
    "AAAA records, one name at a time",
    "fixes #561, #559",
    "Note: this is fine",
    "ratio 3:2:1",
    "Production-Release-Checklist-v12 is out",
    "path src/waypoint/scheduler/CatchUpAfterOutage2.py",
]


@pytest.mark.parametrize("text", REFUSED)
def test_refused(text):
    assert content.problem(text), text


@pytest.mark.parametrize("text", ALLOWED)
def test_allowed(text):
    assert not content.problem(text), text


@pytest.mark.parametrize("url", LINKS_ALLOWED)
def test_links_allowed(url):
    assert content.check_fields({"links.0.url": url}) is None, url


def test_commit_ids_are_allowed_only_in_link_paths():
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert content.check_fields({"note": f"fixed in {sha}"}) == "note"
    assert content.check_fields({"links.0.label": sha}) == "links.0.label"


@pytest.mark.parametrize("url", LINKS_REFUSED)
def test_links_refused(url):
    assert content.check_fields({"links.0.url": url}) == "links.0.url", url


@pytest.mark.parametrize("field,body", [
    ("title", {"title": "token ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2"}),
    ("label", {"label": "192.0.2.10"}),
    ("note", {"note": "mail someone@example.org"}),
    ("question.text", {"kind": "decision", "question": {"text": "use 2001:db8::1?"}}),
    ("question.options.1", {"kind": "decision", "question": {"text": "Which?", "options": ["a", "c0ffee" * 6]}}),
    ("question.recommendation", {"kind": "decision", "question": {"text": "Which?", "recommendation": "a@b.example.org"}}),
    ("links.0.url", {"links": [{"url": "https://example.org/x?sig=abc"}]}),
    ("links.0.label", {"links": [{"url": "https://example.org/x", "label": "198.51.100.7"}]}),
])
def test_every_field_is_checked_on_create(store, who, tracks, field, body):
    full = {"track": "release", "title": "An item", "kind": "task", **body}
    owner = "alice" if full["kind"] == "decision" else "agent-a"
    with pytest.raises(ApiError) as e:
        store.create_item(who("agent-a"), {**full, "owner": owner})
    assert e.value.code == "content" and e.value.field == field
    assert "example.org" not in e.value.message and "198.51" not in e.value.message


def test_checked_on_update_and_tracks(store, mk, who):
    it = mk("agent-a", "Fine")
    with pytest.raises(ApiError) as e:
        store.patch_item(who("agent-a"), it["id"], {"note": "at 192.0.2.10"})
    assert e.value.field == "note"
    with pytest.raises(ApiError) as e:
        store.put_track(who("agent-a"), "t", {"title": "Fine", "note": "contact someone@example.org"})
    assert e.value.field == "note"


def _rate(tokens, field="note"):
    return sum(content.check_fields({field: f"pasted {t} by mistake"}) == field for t in tokens) / len(tokens)


def test_generated_tokens_are_refused():
    """Tokens like Waypoint's own (secrets.token_urlsafe(32)) and base64 keys, from a seeded generator."""
    import random
    import string

    rng = random.Random(20261114)
    urlsafe = string.ascii_letters + string.digits + "-_"
    std = string.ascii_letters + string.digits + "+/"
    tokens_urlsafe = ["".join(rng.choice(urlsafe) for _ in range(43)) for _ in range(500)]
    tokens_b64 = ["".join(rng.choice(std) for _ in range(43)) + "=" for _ in range(500)]
    assert _rate(tokens_urlsafe) >= 0.97
    assert _rate(tokens_b64) >= 0.97
    assert _rate(tokens_urlsafe, "title") >= 0.97


# Package versions with four dotted parts, and only those (the reviewer's narrower rule).
@pytest.mark.parametrize("text", [
    "8:7.1.1.43+dfsg1-1",
    "7.1.1.43+dfsg1-1",
    "installed 8:7.1.1.43+dfsg1-1+deb13u14; fixed 8:7.1.1.43+dfsg1-1+deb13u15",
    "1.2.3.4~rc1-1",
    "2:10.0.0.1-1",
])
def test_debian_versions_with_four_dotted_parts_are_allowed(text):
    assert not content.problem(text), text


@pytest.mark.parametrize("text", [
    "10.0.0.1",
    "10.0.0.1-1",
    "installed 10.0.0.1-1",
    "version 192.168.1.20",
    "fixed 192.168.1.20",
    "10.0.0.0/8",
    "10.0.0.0/8+x",
    "host 10.0.0.1, and 7.1.1.43+dfsg1-1",
    "::ffff:10.0.0.1+x",
    "2001:db8::1",
])
def test_addresses_near_version_words_are_still_refused(text):
    assert content.problem(text), text
