"""Single source of the app's product version (keep in sync with pyproject.toml).

CalVer of the cut (`YYYY.MM.N`). Release *identity* on a running instance is
`version` + `git_sha` (the exact build), surfaced by `/health` — see docs/deploy.md.
`0.1.0` was a standing placeholder, not a release.
"""
__version__ = "2026.09.13"
