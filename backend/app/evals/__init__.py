"""Golden-set fixtures for generative surfaces (GRPH-224).

JSON cases live under `cases/<surface>/`. The runner in `app.services.evals` loads
them from this package directory so they ship with the app image (the backend
Dockerfile copies `app/`). An empty or missing tree is `absent`, not a clean
suite — see `evals.run`.
"""
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
CASES_DIR = PACKAGE_DIR / "cases"
