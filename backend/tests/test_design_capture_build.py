"""GRPH-1488 — design-capture build must not resurrect fixed absence-reads-as-clean flags."""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUILD_PY = REPO / "scripts/design-capture/build.py"
RUN_SH = REPO / "scripts/design-capture/run.sh"

STALE_FLAG_MARKERS = ("GRPH-942", "GRPH-943", "GRPH-944")


def test_build_py_does_not_emit_stale_absence_flags():
    text = BUILD_PY.read_text(encoding="utf-8")
    for marker in STALE_FLAG_MARKERS:
        assert marker not in text, f"{BUILD_PY.name} still references {marker}"


def test_run_sh_does_not_write_build_harnesses_proposed():
    text = RUN_SH.read_text(encoding="utf-8")
    assert "build-harnesses-proposed" not in text
