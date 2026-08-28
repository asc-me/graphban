"""GRPH-575: the seat a grok child actually reads.

**What these tests can and cannot prove.** That grok reads `.grok/config.toml`, in
TOML, only for a *trusted* folder, is an external fact about a third-party binary. No
unit test establishes it; it was measured, and the measurement is recorded in
`adapters/grok.py`'s docstring and on GRPH-575:

    $ grok mcp add --transport http --scope project graphban <url> --header "X-API-Key: ..."
    File modified: C:\\Users\\Alex\\gbshape\\.grok\\config.toml
    $ cat .grok/config.toml
    [mcp_servers.graphban]
    url = "..."
    enabled = true

    [mcp_servers.graphban.headers]
    X-API-Key = "..."

    # in an UNTRUSTED folder:
    $ grok mcp doctor
    .grok/config.toml  0 servers
    ✗ folder untrusted (repo-local (project-scoped) server not started for an untrusted folder)

    # after `grok --trust ...`:
    ✓ server started (1.0s)
    ✗ handshake failed ... HTTP 401 {"detail":"invalid api key"}   # our server, fake key

What these tests DO hold down is everything between that fact and the child: that the
adapter still names that path, still writes that language, still passes `--trust`, and
that the parentage guard did not become JSON-only when TOML arrived. The version this
was measured against is pinned in `Grok.support`.

`test_a_real_grok_binary_loads_the_seat_we_write` closes the loop where a grok is
actually installed, and skips where one is not — so it never turns absence into a pass.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbfleet import seat as seat_mod  # noqa: E402
from gbfleet.adapters import ADAPTERS  # noqa: E402
from gbfleet.seat import Seat, UnrenderableSeat, WouldDeclareParentage  # noqa: E402
from gbfleet.worktree import SEAT_FILES  # noqa: E402

SEAT = Seat(code="enrol-1", server_url="https://cloud.graphban.dev", api_key="gb_sk_test")


# --- the file grok reads -----------------------------------------------------------

def test_the_grok_seat_is_config_toml_and_not_mcp_json(tmp_path: Path):
    """The original defect, stated as an assertion.

    `.grok/mcp.json` appears nowhere in grok's Config Sources. Writing it produced a
    child with no tools and no error — the seat was discarded in silence.
    """
    path = ADAPTERS["grok"].seat_path(tmp_path)
    assert path == tmp_path / ".grok" / "config.toml", (
        f"grok's seat goes to {path}; grok only reads ./.grok/config.toml for project "
        "scope, and a file it does not read is a child with no tools and no error"
    )


def test_the_grok_seat_is_written_as_toml_not_json(tmp_path: Path):
    """Right filename, wrong language, same silence. `.toml` holding JSON is still a
    file grok cannot parse."""
    assert ADAPTERS["grok"].seat_format == seat_mod.TOML

    path = seat_mod.write(tmp_path / "config.toml", SEAT.mcp_config(), seat_mod.TOML)
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))

    assert "mcp_servers" in parsed, (
        f"rendered {sorted(parsed)}; grok's table is `mcp_servers` (snake_case), and "
        "`mcpServers` parses fine as TOML while loading nothing"
    )
    server = parsed["mcp_servers"]["graphban"]
    assert server["url"] == "https://cloud.graphban.dev/api/mcp"
    assert server["enabled"] is True
    assert server["headers"]["X-API-Key"] == "gb_sk_test"


def test_the_grok_seat_is_in_seat_files_so_salvage_excludes_it(tmp_path: Path):
    """Moving the seat file moves the credential. If SEAT_FILES kept pointing at the
    old name, salvage would commit the new one."""
    relative = str(ADAPTERS["grok"].seat_path(tmp_path).relative_to(tmp_path))
    assert relative in SEAT_FILES


def test_the_old_grok_seat_name_is_still_excluded():
    """A worktree left behind by a pre-GRPH-575 gbfleet still has `.grok/mcp.json` with
    a live key in it. Salvage running from this version must still exclude it."""
    assert ".grok/mcp.json" in SEAT_FILES


def test_grok_launches_with_trust(tmp_path: Path, git_repo: Path):
    """The second half of the defect, and the half with no file to point at.

    Without `--trust` the config is correct, present, and never loaded: an untrusted
    folder starts no project-scoped server and reports nothing to the child.
    """
    from gbfleet.worktree import create

    tree = create(git_repo, tmp_path / "w", "wave", "1")
    launch = ADAPTERS["grok"].launch(SEAT, tree, tmp_path / "prompt.txt", Path("grok"))

    assert "--trust" in launch.argv, (
        "grok launched without --trust: the worktree is untrusted, the project-scoped "
        "MCP server is never started, and the child runs to completion with no tools "
        f"and no complaint. argv={launch.argv}"
    )


# --- the format must not be silently defaulted -------------------------------------

@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_every_adapter_launch_passes_its_own_seat_format(
    name: str, tmp_path: Path, git_repo: Path
):
    """Declaring `seat_format` on the class and forgetting to pass it in `launch()`
    would write JSON to a TOML path and produce exactly the bug this ticket fixes —
    with the class looking correct."""
    from gbfleet.worktree import create

    tree = create(git_repo, tmp_path / f"w-{name}", "wave", "1")
    adapter = ADAPTERS[name]
    launch = adapter.launch(SEAT, tree, tmp_path / "prompt.txt", Path(adapter.binary))

    assert launch.seat_format == adapter.seat_format, (
        f"{name} declares seat_format={adapter.seat_format!r} but its launch() carries "
        f"{launch.seat_format!r}, so the file is written in the wrong language"
    )


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_every_adapter_seat_format_has_a_renderer(name: str):
    assert ADAPTERS[name].seat_format in seat_mod._RENDERERS


def test_an_unknown_format_refuses_rather_than_writing_something(tmp_path: Path):
    target = tmp_path / "seat"
    with pytest.raises(UnrenderableSeat):
        seat_mod.write(target, SEAT.mcp_config(), "yaml")
    assert not target.exists(), "refused the format but left a file behind anyway"


# --- the parentage guard is a property of the config, not of JSON ------------------

@pytest.mark.parametrize("fmt", sorted(seat_mod._RENDERERS))
def test_parentage_is_refused_in_every_format(fmt: str, tmp_path: Path):
    """D-b's guard predates TOML. A guard that only covered the format it was written
    for would be no guard at all the day a second one arrived."""
    config = SEAT.mcp_config()
    config["mcpServers"]["graphban"]["headers"]["parent_agent_id"] = "planner-1"

    target = tmp_path / f"seat.{fmt}"
    with pytest.raises(WouldDeclareParentage):
        seat_mod.write(target, config, fmt)
    assert not target.exists(), "refused the config but wrote the file first"


# --- the renderer refuses what it cannot say ---------------------------------------

def test_a_field_grok_cannot_express_raises_instead_of_vanishing(tmp_path: Path):
    """Dropping an unknown key would hand the child a seat missing exactly the thing
    that was just added to `mcp_config`, and nothing would report it."""
    config = SEAT.mcp_config()
    config["mcpServers"]["graphban"]["oauth"] = {"client_id": "x"}
    with pytest.raises(UnrenderableSeat, match="oauth"):
        seat_mod.write(tmp_path / "config.toml", config, seat_mod.TOML)


def test_a_stdio_server_is_refused_rather_than_turned_into_an_http_one(tmp_path: Path):
    """grok reads the transport off `url`, so there is no way to say `stdio` here.
    Rendering it anyway would quietly change what the child connects to."""
    config = SEAT.mcp_config()
    config["mcpServers"]["graphban"]["type"] = "stdio"
    with pytest.raises(UnrenderableSeat, match="stdio"):
        seat_mod.write(tmp_path / "config.toml", config, seat_mod.TOML)


def test_a_credential_with_a_quote_in_it_still_parses(tmp_path: Path):
    """The key and url come from the server. An unescaped `"` or backslash emits a file
    grok fails to parse, which from the child's side is indistinguishable from having
    no seat at all."""
    nasty = Seat(
        code="c",
        server_url='https://example.test/a"b\\c',
        api_key='gb_sk_"quote\\back\nnewline\ttab',
    )
    path = seat_mod.write(tmp_path / "config.toml", nasty.mcp_config(), seat_mod.TOML)
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))

    server = parsed["mcp_servers"]["graphban"]
    assert server["headers"]["X-API-Key"] == 'gb_sk_"quote\\back\nnewline\ttab'
    assert server["url"] == 'https://example.test/a"b\\c/api/mcp'


def test_a_header_name_that_is_not_a_bare_key_is_quoted(tmp_path: Path):
    config = SEAT.mcp_config()
    config["mcpServers"]["graphban"]["headers"] = {"X Weird.Header": "v"}
    path = seat_mod.write(tmp_path / "config.toml", config, seat_mod.TOML)
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["mcp_servers"]["graphban"]["headers"]["X Weird.Header"] == "v"


def test_the_seat_file_is_still_0600_in_toml(tmp_path: Path):
    path = seat_mod.write(tmp_path / "config.toml", SEAT.mcp_config(), seat_mod.TOML)
    assert path.stat().st_mode & 0o777 == 0o600


# --- the loop closed against a real binary, where there is one ---------------------

def _probe(
    tmp_path: Path,
    *,
    trusted: bool,
    user_scope_url: str = "",
    committed_url: str = "",
    seat: bool = True,
) -> str:
    """Write the seat exactly as the adapter would and ask grok's own doctor about it.

    Runs under an isolated HOME. Without that, `grok mcp doctor` also reports every
    server in the operator's real config — and an assertion looking for "server started"
    anywhere in that output passes on `serena` or `vercel` while proving nothing about
    the file under test. That is not hypothetical; it is how the first version of this
    test passed.

    Trust is pre-seeded into the isolated store rather than granted with `--trust`,
    because `--trust` only records during a real session and a real session is a model
    call. The adapter's use of `--trust` is asserted separately, in argv.
    """
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".grok").mkdir(parents=True)
    project.mkdir()

    adapter = ADAPTERS["grok"]
    if seat:
        probe = Seat(code="c", server_url="https://seat.invalid", api_key="gb_sk_NOT_REAL")
        seat_mod.write(adapter.seat_path(project), probe.mcp_config(), adapter.seat_format)

    if user_scope_url:
        (home / ".grok" / "config.toml").write_text(
            f'[mcp_servers.graphban]\nurl = "{user_scope_url}"\nenabled = true\n',
            encoding="utf-8",
        )
    if committed_url:
        # grok also reads `.mcp.json` and `.cursor/mcp.json` from the project directory
        # (measured — `grok mcp doctor` starts servers from both while its "Config
        # sources" block credits neither correctly). A worktree is cut from the repo, so
        # anything the repo commits lands in every child.
        (project / ".cursor").mkdir()
        (project / ".cursor" / "mcp.json").write_text(
            json.dumps({"mcpServers": {"graphban": {"type": "http", "url": committed_url}}}),
            encoding="utf-8",
        )
    if trusted:
        (home / ".grok" / "trusted_folders.toml").write_text(
            f"[folders.'{project}']\ntrusted = true\ndecided_at = 1787935094\n",
            encoding="utf-8",
        )

    doctor = subprocess.run(
        ["grok", "mcp", "doctor"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "HOME": str(home)},
    )
    return doctor.stdout + doctor.stderr


def _graphban_block(out: str) -> str:
    """Just the `graphban (...)` stanza. Asserting against the whole output is how an
    unrelated server's success gets read as this one's."""
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("graphban ("):
            block = [line]
            for following in lines[i + 1:]:
                if following.strip() and not following.startswith((" " * 4, "\t")):
                    break
                if following.strip() and not following.strip().startswith(("✓", "✗", "→")):
                    break
                block.append(following)
            return "\n".join(block)
    return ""


@pytest.mark.skipif(shutil.which("grok") is None, reason="no grok binary on this machine")
def test_a_real_grok_binary_starts_the_server_from_the_seat_we_write(tmp_path: Path):
    """The one test here that touches the external fact.

    The key is deliberately invalid and the host deliberately unresolvable: the claim is
    that grok READ and STARTED our file, never that the credential works. Nothing real
    is contacted.
    """
    block = _graphban_block(_probe(tmp_path, trusted=True))
    assert block, "grok's doctor never mentioned our server at all"
    assert "server started" in block, (
        "grok did not start the server from the seat this adapter writes — a child "
        f"would run with no tools and no error.\n{block}"
    )


@pytest.mark.skipif(shutil.which("grok") is None, reason="no grok binary on this machine")
def test_the_same_seat_in_an_untrusted_folder_does_not_start(tmp_path: Path):
    """The negative control, and the reason to believe the test above.

    Without it, `test_...starts_the_server...` is a green light with no demonstrated
    ability to go red — which is the failure mode this whole ticket is about. This is
    also the original defect stated directly: correct file, correct language, and the
    server silently not started.
    """
    block = _graphban_block(_probe(tmp_path, trusted=False))
    assert block, "grok's doctor never mentioned our server at all"
    assert "folder untrusted" in block, (
        "an untrusted folder was expected to refuse the repo-local server. If grok "
        f"stopped gating on trust, --trust is now dead weight and this must be revisited.\n{block}"
    )
    assert "server started" not in block


@pytest.mark.skipif(shutil.which("grok") is None, reason="no grok binary on this machine")
def test_a_childs_seat_beats_an_operators_user_level_server_of_the_same_name(tmp_path: Path):
    """Security-relevant, and not obvious enough to assume.

    An operator who has configured `graphban` in their own `~/.grok/config.toml` is the
    normal case — they are the one running the supervisor. If user scope won a name
    collision, every child would connect with the OPERATOR's credential and take the
    operator's role, and seat-based roles would mean nothing while looking like they
    worked.

    Measured: project scope wins.
    """
    block = _graphban_block(
        _probe(tmp_path, trusted=True, user_scope_url="https://operator-scope.invalid/api/mcp")
    )
    assert "seat.invalid" in block, (
        "the child resolved `graphban` to something other than its own seat. If user "
        "scope now wins, a child inherits the operator's credential and role.\n" + block
    )
    assert "operator-scope.invalid" not in block


@pytest.mark.skipif(shutil.which("grok") is None, reason="no grok binary on this machine")
def test_the_seat_beats_an_mcp_file_committed_to_the_repository(tmp_path: Path):
    """The other way a child could be handed the wrong server, and the likelier one.

    grok reads `.cursor/mcp.json` and `.mcp.json` from the project directory as well as
    its own `.grok/config.toml`. A worktree is cut from the repo, so a repository that
    commits either — entirely reasonable, and this repo commits `.cursor/agents/` today
    — puts that file in front of every child. If a committed `graphban` entry won, the
    fleet's seats would be overridden by a checked-in file and every child would share
    one credential.

    Measured: the seat wins. Asserted here because it is the repo's own contents that
    would break it, and repo contents change.
    """
    block = _graphban_block(
        _probe(tmp_path, trusted=True, committed_url="https://committed-file.invalid/api/mcp")
    )
    assert "seat.invalid" in block, (
        "a committed .cursor/mcp.json overrode the child's seat — every child would "
        "share whatever credential is checked into the repository.\n" + block
    )
    assert "committed-file.invalid" not in block


@pytest.mark.skipif(shutil.which("grok") is None, reason="no grok binary on this machine")
def test_a_committed_mcp_file_really_is_loaded_when_nothing_outranks_it(tmp_path: Path):
    """The control for the test above, and the reason to believe it.

    `test_the_seat_beats_an_mcp_file_committed_to_the_repository` would pass just as
    green if grok ignored `.cursor/mcp.json` entirely — "the seat won" and "the rival
    was never in the race" produce identical output. This shows the rival is real: with
    no seat present, the committed file is what the child gets.

    Which is also the operational warning. A repo that commits an MCP file gives every
    grok child those servers, on top of its seat. Nothing in gbfleet controls that.
    """
    block = _graphban_block(
        _probe(
            tmp_path,
            trusted=True,
            seat=False,
            committed_url="https://committed-file.invalid/api/mcp",
        )
    )
    assert "committed-file.invalid" in block, (
        "grok did not load a committed .cursor/mcp.json at all, so the precedence test "
        "above is proving nothing. Either grok changed, or the probe stopped writing "
        f"the file.\n{block}"
    )
