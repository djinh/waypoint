"""Copy docs/api/openapi.yaml to src/waypoint/openapi.json, which the daemon validates requests with,
after setting the contract's `info.version` to the package's version.

    python scripts/sync_openapi.py

The YAML file is the one to edit (except its version, which this sets from
src/waypoint/__init__.py); a test fails when either is out of date. Needs PyYAML.
"""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
YAML = ROOT / "docs" / "api" / "openapi.yaml"

if __name__ == "__main__":
    version = re.search(r'^__version__ = "([^"]+)"', (ROOT / "src" / "waypoint" / "__init__.py").read_text(),
                        re.M).group(1)
    text = YAML.read_text(encoding="utf-8")
    new = re.sub(r"^(info:\n(?:  .*\n)*?  version: ).*$", lambda m: m.group(1) + version, text, count=1, flags=re.M)
    if new != text:
        YAML.write_text(new, encoding="utf-8")
    spec = yaml.safe_load(new)
    out = ROOT / "src" / "waypoint" / "openapi.json"
    out.write_text(json.dumps(spec, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
