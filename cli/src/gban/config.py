"""Where `gban` keeps its two facts, and why they are two files (PRD-40 D3, D10).

`~/.graphban/` is shared with `graphban`, the operator's database-side tool, and the sharing
stops at the directory. `gban` reads `gban.json` (`url`, `project`) and `session.json` (a refresh
token) and **never opens `config.json`**, which is `graphban`'s and may hold a database link.

A third file, `supervisor.json`, holds the project-scoped **agent** keys `gban setup` minted
(`gb_sk_…`). It is not the login session. `gban fleet` / `gban doctor` hand that key to
`gbfleet`; feeding the refresh token instead is how a working session produces "session
expired" on the supervisor (GRPH-782).

The grill made that stricter than the draft. Reading `config.json` and ignoring the keys it did
not recognise would have been true and insufficient: the risk is not misreading a database
password, it is that password living in a file which now has a second consumer and a second
reason to be copied onto another machine. `graphban` runs in a container against a database;
`gban` runs on a laptop against HTTP; the credential that must not cross that line lives in its
own file, so copying a config never carries it.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

#: The directory both tools use. Shared deliberately — a person has one Graphban.
HOME_ENV = "GRAPHBAN_HOME"

#: `gban`'s own settings. NOT `config.json`, which belongs to `graphban` (D10).
SETTINGS_FILE = "gban.json"

#: The refresh token, alone in its own file so that copying settings never carries it.
SESSION_FILE = "session.json"

#: Project-scoped agent keys `gban setup` minted. Next to the session, never inside it —
#: a refresh token and a `gb_sk_…` are different credentials, and mixing them is the
#: remaining hole GRPH-782 was reopened for.
SUPERVISOR_KEYS_FILE = "supervisor.json"

#: `graphban`'s file. Named here only so the test that asserts `gban` never opens it has
#: something to name, and so a reader knows the omission is deliberate.
NOT_OURS = "config.json"

#: What a minted agent key looks like. `gbfleet` wants this; a user refresh token is not it.
AGENT_KEY_PREFIX = "gb_sk_"

URL_ENV = "GRAPHBAN_URL"
PROJECT_ENV = "GRAPHBAN_PROJECT"
API_KEY_ENV = "GRAPHBAN_API_KEY"

#: Owner read/write and nothing else. A credential at rest gets the same mode the seat files
#: in `gbfleet` get, for the same reason.
PRIVATE = stat.S_IRUSR | stat.S_IWUSR


def home() -> Path:
    return Path(os.environ.get(HOME_ENV) or (Path.home() / ".graphban"))


def _read(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A missing file and an unreadable one are the same to a caller that has a default,
        # and neither is worth a traceback in front of somebody trying to log in.
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Created private BEFORE anything is written to it. Writing first and chmod-ing after
    # leaves a window where the token is world-readable, which is the whole failure.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.chmod(path, PRIVATE)
    return path


def settings() -> dict:
    return _read(home() / SETTINGS_FILE)


def save_settings(**values: str) -> Path:
    merged = {k: v for k, v in {**settings(), **values}.items() if v}
    return _write(home() / SETTINGS_FILE, merged)


def session() -> dict:
    return _read(home() / SESSION_FILE)


def save_session(refresh_token: str, *, user: str = "") -> Path:
    return _write(home() / SESSION_FILE,
                  {"refresh_token": refresh_token, "user": user})


def clear_session() -> bool:
    path = home() / SESSION_FILE
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _agent_key(value: str) -> str:
    value = (value or "").strip()
    return value if value.startswith(AGENT_KEY_PREFIX) else ""


def save_supervisor_key(project: str, key: str) -> Path | None:
    """Store the project-scoped agent key `gban setup` minted. Refuses a refresh token."""
    key = _agent_key(key)
    if not project or not key:
        return None
    blob = _read(home() / SUPERVISOR_KEYS_FILE)
    keys = blob.get("keys") if isinstance(blob.get("keys"), dict) else {}
    keys[project] = key
    return _write(home() / SUPERVISOR_KEYS_FILE, {"keys": keys})


def stored_supervisor_key(project: str) -> str:
    """The agent key setup stored for this project, or empty. Never reads `session.json`."""
    if not project:
        return ""
    blob = _read(home() / SUPERVISOR_KEYS_FILE)
    keys = blob.get("keys") if isinstance(blob.get("keys"), dict) else {}
    return _agent_key(str(keys.get(project) or ""))


def resolve(flag: str | None, env: str, key: str) -> str:
    """D10's precedence, in one place: flag, then environment, then the settings file.

    One function rather than three lookups at each call site, because a precedence that is
    re-implemented per option is one that will eventually differ per option.
    """
    return (flag or os.environ.get(env) or settings().get(key) or "").strip()
