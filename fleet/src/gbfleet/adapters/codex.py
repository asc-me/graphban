"""Codex — declared, and deliberately NOT implemented.

PRD-22 S2 names four vendors. Three are implemented against a binary that was actually
run: their version strings, their flags and their config mechanisms were read off
`--help` rather than recalled. Codex was not installed on the machine this was written
on, so every one of those four facts would have been invented.

**A fabricated adapter is worse than a missing one**, and the failure mode is the exact
one S2 exists to prevent: a child that starts, does not understand its arguments, and
never registers — burning a registration window and blaming the vendor for a mistake the
supervisor made. Refusing by name costs a clear error message; guessing costs money and
misattributes the fault.

So `resolve("codex")` raises `UnknownAdapter` and lists the vendors that do work. When
somebody has the binary in front of them, finishing this is four declarations and a
version string.
"""

from __future__ import annotations


class Codex:
    """Placeholder. Never registered — see the module docstring."""

    name = "codex"
    binary = "codex"
    implemented = False
