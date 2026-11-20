"""The systemd unit examples keep their safety properties."""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

UNITS = Path(__file__).resolve().parent.parent / "systemd"


def unit(name: str) -> dict:
    cp = configparser.ConfigParser(strict=False, interpolation=None, comment_prefixes=("#",))
    cp.optionxform = str
    cp.read(UNITS / name)
    return {s: dict(cp[s]) for s in cp.sections()}


def test_daemon_unit_is_hardened():
    svc = unit("waypoint.service")["Service"]
    assert svc["User"] == "waypoint" and svc["StateDirectory"] == "waypoint"
    for key, value in {"NoNewPrivileges": "yes", "ProtectSystem": "strict", "ProtectHome": "yes",
                       "PrivateTmp": "yes", "CapabilityBoundingSet": "", "Restart": "always"}.items():
        assert svc[key] == value, key
    assert "serve" in svc["ExecStart"]


@pytest.mark.parametrize("name", ["waypoint.service", "waypoint-backup.service", "waypoint-deliver.service",
                                  "waypoint-backup.timer"])
def test_no_secrets_or_wide_binds(name):
    text = (UNITS / name).read_text()
    assert "0.0.0.0" not in text and "Environment" not in text and "token" not in text.lower()


def test_backup_unit_has_no_network():
    svc = unit("waypoint-backup.service")["Service"]
    assert svc["PrivateNetwork"] == "yes" and svc["User"] == "waypoint" and "backup" in svc["ExecStart"]


@pytest.mark.parametrize("name", ["waypoint.service", "waypoint-backup.service"])
def test_state_directory_stays_private(name):
    # without StateDirectoryMode systemd resets the directory to 0755 each time the unit starts
    svc = unit(name)["Service"]
    assert svc["StateDirectory"] == "waypoint" and svc["StateDirectoryMode"] == "0700"

