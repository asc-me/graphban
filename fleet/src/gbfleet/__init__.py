"""The Graphban fleet supervisor.

A thin local client that runs where the agents actually run: it spawns vendor CLI
processes holding seats the *server* issued, and it reaps them. It holds no authority
of its own (PRD-22 §4) — delete it and every invariant still holds, the fleet just
needs a human to open terminals again.
"""

from importlib.metadata import PackageNotFoundError, version as _installed_version

#: Reported by `gbfleet --version`, and the string a bug report will quote.
#:
#: Read from installed package metadata rather than written here, so there is exactly
#: one place the version is stated (`pyproject.toml`) and no second copy to drift from
#: it. The fallback is deliberately NOT a plausible version: running from a source tree
#: that was never installed is a real situation, and it must not report something a
#: reader would mistake for a release. An absence has to look like one.
UNINSTALLED = "0+not-installed"

try:
    __version__ = _installed_version("graphban-fleet")
except PackageNotFoundError:  # pragma: no cover - exercised by test_packaging
    __version__ = UNINSTALLED

__all__ = ["__version__", "UNINSTALLED"]
