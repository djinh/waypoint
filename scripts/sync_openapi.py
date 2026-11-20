"""Copy docs/api/openapi.yaml to src/waypoint/openapi.json, which the daemon validates requests with.

    python scripts/sync_openapi.py

The YAML file is the one to edit; a test fails when the JSON copy is out of date. Needs PyYAML.
"""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    spec = yaml.safe_load((ROOT / "docs" / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    out = ROOT / "src" / "waypoint" / "openapi.json"
    out.write_text(json.dumps(spec, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
