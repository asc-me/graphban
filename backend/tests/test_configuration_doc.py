"""Every setting is documented, or says why not (GRPH-469).

`docs/configuration.md` presents itself as the configuration reference — *"Configuration is
via environment variables… The backend reads them through `backend/app/config.py`"*. Measured
2026-08-22, it named **17 of 51** settings.

The omissions decided whether an instance was safe: `SECRET_ENCRYPTION_KEY` (at-rest
encryption for stored provider keys), `REQUIRE_STRONG_SECRET`, `PLATFORM_ADMIN_EMAILS`,
`SIGNUP_MODE` — the hosted deployment runs `invite_only` and the document could not tell you
the setting exists — `TRUSTED_PROXY`, `GITHUB_WEBHOOK_SECRET`.

**The failure is not that the doc was incomplete.** It is that nothing in it said so, so its
silence read as "there is nothing else to set". Someone deploying from it could not discover
what they had not configured.

This test is the part that stops it recurring. A one-time cleanup starts rotting the day it
merges; a new setting should fail here until somebody decides whether an operator needs to
know about it. Same shape as the tool-role ratchet in `test_authority_gates.py`, and the same
reason.
"""
from __future__ import annotations

import pathlib
import re

from app.config import Settings

DOC = pathlib.Path(__file__).resolve().parents[2] / "docs" / "configuration.md"

#: Settings deliberately absent from the reference, with the reason. An entry here is a
#: decision on the record; an entry missing from BOTH this and the doc is the defect.
UNDOCUMENTED: dict[str, str] = {
    "git_sha": "build-injected, not operator-settable — baked in at image build",
    "railway_git_commit_sha": "platform-injected by Railway, same as GIT_SHA",
}


def _documented() -> set[str]:
    text = DOC.read_text()
    return {name for name in Settings.model_fields
            if re.search(rf"\b{name.upper()}\b", text)}


def test_every_setting_is_documented_or_excused():
    """THE guard. An operator reads this document to configure an instance, and a setting in
    neither place is one they cannot discover."""
    documented = _documented()
    unknown = sorted(n for n in Settings.model_fields
                     if n not in documented and n not in UNDOCUMENTED)
    assert not unknown, (
        f"undocumented settings: {[n.upper() for n in unknown]} — add them to "
        "docs/configuration.md, or to UNDOCUMENTED here with the reason an operator does "
        "not need to know")


def test_the_excuse_list_only_covers_settings_that_exist():
    """A stale excuse argues about a setting nobody can set, and hides the fact that the
    reason was never re-examined."""
    gone = sorted(n for n in UNDOCUMENTED if n not in Settings.model_fields)
    assert not gone, f"excused but no longer a setting: {gone}"


def test_every_excuse_gives_a_reason():
    """An empty reason is the omission wearing a disguise."""
    thin = {n: r for n, r in UNDOCUMENTED.items() if len(r.strip()) < 20}
    assert not thin, f"no real reason given: {sorted(thin)}"


def test_the_excuse_list_stays_short():
    """A RATCHET. The list exists for settings an operator genuinely cannot set — both of
    them today are injected at build time. If it grows, "undocumented" has quietly become a
    category rather than an exception."""
    assert len(UNDOCUMENTED) <= 2, (
        f"{len(UNDOCUMENTED)} settings are excused: {sorted(UNDOCUMENTED)}. Document the new "
        "one instead.")


def test_the_settings_that_decide_whether_an_instance_is_safe_are_named():
    """Named individually rather than trusted to the count above, because these are the ones
    the ticket was opened about — a future edit that drops one should fail loudly and not
    just move a number."""
    text = DOC.read_text()
    for name in ("SECRET_ENCRYPTION_KEY", "REQUIRE_STRONG_SECRET", "PLATFORM_ADMIN_EMAILS",
                 "SIGNUP_MODE", "TRUSTED_PROXY", "GITHUB_WEBHOOK_SECRET",
                 "MIN_PASSWORD_LENGTH", "LOGIN_RATE_PER_MIN"):
        assert re.search(rf"\b{name}\b", text), f"{name} is not in the configuration reference"


def test_the_dangerous_defaults_say_they_are_dangerous():
    """Naming a setting is not the same as documenting it. `SIGNUP_MODE` defaults to `open`
    and `SECRET_ENCRYPTION_KEY` to empty; a row that lists the default without saying what it
    means leaves the reader exactly where they were."""
    text = DOC.read_text()
    row = next(line for line in text.splitlines() if "`SIGNUP_MODE`" in line)
    assert "invite_only" in row and "anyone" in row.lower()
    row = next(line for line in text.splitlines() if "`SECRET_ENCRYPTION_KEY`" in line)
    assert "unencrypted" in row.lower()


def test_the_doc_only_names_settings_that_exist():
    """The reverse direction, currently clean and worth keeping so. A reference naming a
    variable the app has never read sends an operator to set something with no effect.

    Compose-level names (`API_PORT`, `WEB_PORT`, `DB_PORT`) are deliberately excluded: they
    are real, and they are not `Settings` fields.
    """
    text = DOC.read_text()
    compose = {"API_PORT", "WEB_PORT", "DB_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD",
               "POSTGRES_DB", "VITE_API_BASE", "NODE_ENV", "PORT", "API_UPSTREAM",
               "API_SCHEME", "NGINX_LOCAL_RESOLVERS", "GRAPHBAN_KEY", "AGENTLEDGER_KEY",
               "GRAPHBAN_CONFIG", "AGENTLEDGER_CONFIG", "DATABASE_URL", "SEED_ON_START",
               "CREDENTIAL_RETRY_SECONDS", "NPM_CONFIG_MINIMUM_RELEASE_AGE",
               # Real, and read by the `anthropic` SDK itself rather than through Settings —
               # which the document already says in its own Notes column.
               "ANTHROPIC_API_KEY"}
    known = {n.upper() for n in Settings.model_fields} | compose
    # Only the FIRST cell of a table row — the Var column. Scanning the whole row picks up
    # DEFAULTS (`HS256`, `INFO`) and reads them as variable names, which is a test failing on
    # its own parsing rather than on the document.
    cited: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("| `"):
            continue
        var_cell = line.split("|")[1]
        cited |= {m.group(1) for m in re.finditer(r"`([A-Z][A-Z0-9_]{3,})`", var_cell)}
    assert not (cited - known), f"documented but not a setting: {sorted(cited - known)}"
