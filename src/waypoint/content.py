"""The content check: free text must not hold secrets, addresses or personal data.

Many agents and pages read every item, so Waypoint refuses the obvious cases when an item or
track is written. It is a net for mistakes, not a guarantee: it errs on the side of refusing,
and the refusal names the field, never the text.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from .errors import ApiError

_PATTERNS = [
    # long hex strings: hashes, keys, device ids
    re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32,}(?![0-9a-fA-F])"),
    # known token shapes
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._~+/-]{16,}"),
    # signed or credentialed URLs
    re.compile(r"[?&](?:X-Amz-Signature|X-Amz-Credential|sig|signature|token|access_token|api_key|apikey|key)=", re.I),
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),
    # e-mail addresses
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    # UUIDs (session, device and account ids) and MAC addresses
    re.compile(r"(?<![0-9A-Fa-f-])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
               r"(?![0-9A-Fa-f-])"),
    re.compile(r"(?<![0-9A-Fa-f:-])[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4}(?![0-9A-Fa-f:-])"),
]

# Long runs of mixed letters and digits look like random tokens ("a8Fq3..."). "/", "-" and "."
# separate runs, so URL paths, slugs and dates don't add up to one long "token".
_TOKENISH = re.compile(r"[A-Za-z0-9_+=]{32,}")

_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\d.])")
_IPV6 = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:/\d{1,3})?(?![0-9A-Fa-f:])")


# Base64 and URL-safe tokens contain "-", "/", "+" and "="; a run over this wider class counts
# when it mixes cases and digits the way random text does (see _mixed_random).
_MIXED = re.compile(r"[A-Za-z0-9_\-+=/]{32,}")
_MIXED_NO_SLASH = re.compile(r"[A-Za-z0-9_\-+=]{32,}")


def _looks_random(s: str) -> bool:
    return bool(re.search(r"[A-Za-z]", s)) and bool(re.search(r"\d", s)) and (
        sum(c.isdigit() for c in s) >= 4
    )


def _class(c: str) -> int:
    return 0 if c.isupper() else 1 if c.islower() else 2 if c.isdigit() else 3


def _mixed_random(s: str) -> bool:
    """Upper and lower case, at least two digits, and the character class changing at least every
    2.5 characters on average. Random tokens change class about two times in three; words, slugs
    and CamelCase names change far less often ("Production-Release-Checklist-v12": one in four)."""
    if not (re.search(r"[A-Z]", s) and re.search(r"[a-z]", s) and sum(c.isdigit() for c in s) >= 2):
        return False
    changes = sum(_class(a) != _class(b) for a, b in zip(s, s[1:]))
    return changes >= 0.4 * (len(s) - 1)


# A Debian package version may hold four dotted numbers (ImageMagick's 7.1.1.43). Only two contexts
# make such a number a version, never an address: a Debian epoch right before it ("8:7.1.1.43"), or a
# suffix right after it that starts with "+" or "~" ("7.1.1.43+dfsg1-1", "1.2.3.4~rc1"). A word such as
# "installed" before it, or a plain "-1" after it, doesn't: "installed 10.0.0.1-1" stays refused.
_EPOCH_BEFORE = re.compile(r"(?:^|[^\w:.])\d+:$")


def _is_version(text: str, m: re.Match) -> bool:
    if "/" in m.group(0):
        return False  # a CIDR prefix is never a version
    after = text[m.end():m.end() + 1]
    return after in ("+", "~") or bool(_EPOCH_BEFORE.search(text[:m.start()]))


def _has_address(text: str) -> bool:
    for m in _IPV4.finditer(text):
        if _is_version(text, m):
            continue
        try:
            ipaddress.ip_network(m.group(0), strict=False)
            return True
        except ValueError:
            continue
    for m in _IPV6.finditer(text):
        cand = m.group(0)
        if cand.count(":") < 2 or sum(c in "0123456789abcdefABCDEF" for c in cand) < 4:
            continue
        try:
            ipaddress.ip_network(cand, strict=False)
            return True
        except ValueError:
            continue
    return False


def problem(text: str, slash_separates: bool = False) -> bool:
    """True if the text looks like it holds something that must not be stored.

    ``slash_separates``: in a link's URL, "/" separates path segments instead of being part of a
    possible base64 token.
    """
    if any(p.search(text) for p in _PATTERNS):
        return True
    if any(_looks_random(m.group(0)) for m in _TOKENISH.finditer(text)):
        return True
    mixed = _MIXED_NO_SLASH if slash_separates else _MIXED
    if any(_mixed_random(m.group(0)) for m in mixed.finditer(text)):
        return True
    return _has_address(text)


# A git object id: SHA-1 (abbreviated from 7 characters, or full) or SHA-256.
_COMMIT_ID = re.compile(r"^(?:[0-9a-fA-F]{7,40}|[0-9a-fA-F]{64})$")
# A UUID as a whole path segment of a link names a page (a document, a ticket), as it does today.
_UUID_SEGMENT = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
_COMMIT_WORDS = {"commit", "commits", "blob", "tree"}


def link_problem(url: str) -> bool:
    """Like problem(), with two allowances in a link's URL: a commit id as the path segment right
    after /commit/, /commits/, /blob/ or /tree/, and a UUID as a whole path segment. Any other hex
    in a URL (webhook and capability URLs often carry their secret in the path) and the query string
    are checked as usual.
    A URL that can't be parsed at all (e.g. an unclosed "[") raises ValueError.
    """
    parts = urlsplit(url)
    segs = parts.path.split("/")
    kept = [
        "x" if (i > 0 and segs[i - 1].lower() in _COMMIT_WORDS and _COMMIT_ID.match(seg))
        or _UUID_SEGMENT.match(seg) else seg
        for i, seg in enumerate(segs)
    ]
    return problem(urlunsplit((parts.scheme, parts.netloc, "/".join(kept), parts.query, parts.fragment)),
                   slash_separates=True)


def check_fields(fields: dict[str, str | None]) -> str | None:
    """The name of the first field that fails the check, or None."""
    for name, text in fields.items():
        if not text:
            continue
        try:
            bad = link_problem(text) if name.startswith("links.") and name.endswith(".url") else problem(text)
        except ValueError:
            raise ApiError("invalid", f"{name}: not a valid URL", field=name) from None
        if bad:
            return name
    return None
