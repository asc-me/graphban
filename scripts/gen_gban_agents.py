#!/usr/bin/env python3
"""Write `web/src/lib/gban-agents.json` from the CLI's `gban.setup.SETUP_AGENTS` (GRPH-1362).

The quick-start dialog offers the `gban setup` route only to agents the CLI can configure, so
the list is exported from the CLI package and never copied into web/ by hand.
`cli/tests/test_setup.py` fails when the committed file differs; regenerate with this script.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "src" / "lib" / "gban-agents.json"
sys.path.insert(0, str(ROOT / "cli" / "src"))


def render() -> str:
    from gban.setup import SETUP_AGENTS
    return json.dumps({"agents": list(SETUP_AGENTS)}, indent=2) + "\n"


if __name__ == "__main__":
    OUT.write_text(render())
    print(f"wrote {OUT.relative_to(ROOT)}")
