"""AL-213: the generated sub-agent fleet stays consistent + AGENTS.md-sourced.

Guards `scripts/gen_subagents.py`:
  1. The committed files match the generator (nobody hand-edited the output).
  2. Each toolchain gets its **native** format — Cursor & Claude Code Markdown with
     their own frontmatter, Codex a valid TOML role file — while the prompt body is
     shared across all three (one source, native output per tool).
  3. Read-only intent maps to each tool's native control.
  4. The fleet README's invariants are the *verbatim* AGENTS.md invariants (anti-drift).
"""
import importlib.util
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GEN = REPO / "scripts" / "gen_subagents.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_subagents", GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_fleet_matches_generator():
    gen = _load_generator()
    for rel, expected in gen.render_files().items():
        path = REPO / rel
        assert path.exists(), f"{rel} missing — run scripts/gen_subagents.py"
        assert path.read_text() == expected, f"{rel} stale — run scripts/gen_subagents.py"


def test_each_toolchain_gets_its_native_format():
    gen = _load_generator()
    files = gen.render_files()

    # Cursor: Markdown + Cursor frontmatter (model/readonly/is_background).
    cur = files[".cursor/agents/gb-implementer.md"]
    assert cur.startswith("---\n")
    assert "model: composer-2" in cur and "readonly: false" in cur and "is_background:" in cur

    # Claude Code: Markdown + Claude frontmatter — native model, and NONE of the
    # Cursor-only fields (which Claude Code would not understand).
    cl = files[".claude/agents/gb-implementer.md"]
    assert cl.startswith("---\n")
    assert "model: haiku" in cl
    assert "readonly:" not in cl and "is_background:" not in cl and "composer-2" not in cl

    # Codex: a TOML role file, not Markdown — no stale .md emitted for Codex.
    assert ".codex/agents/gb-implementer.md" not in files
    cx = files[".codex/agents/gb-implementer.toml"]
    assert "developer_instructions = '''" in cx and 'model_reasoning_effort = "low"' in cx


def test_prompt_body_is_shared_across_toolchains():
    """Only the format/frontmatter differs — the instruction body is one source."""
    gen = _load_generator()
    for role in gen.ROSTER:
        body = role["body"]
        assert body in gen.render_cursor(role)
        assert body in gen.render_claude(role)
        assert body in gen.render_codex(role)


def test_readonly_maps_to_each_tools_native_control():
    gen = _load_generator()
    scout = next(r for r in gen.ROSTER if r["name"] == "gb-scout")  # read-only
    impl = next(r for r in gen.ROSTER if r["name"] == "gb-implementer")  # writer

    assert "readonly: true" in gen.render_cursor(scout)
    assert "readonly: false" in gen.render_cursor(impl)
    assert 'sandbox_mode = "read-only"' in gen.render_codex(scout)
    assert 'sandbox_mode = "workspace-write"' in gen.render_codex(impl)


def test_codex_toml_is_valid_and_round_trips_the_body():
    gen = _load_generator()
    for role in gen.ROSTER:
        data = tomllib.loads(gen.render_codex(role))
        assert data["name"] == role["name"]
        assert data["sandbox_mode"] in ("read-only", "workspace-write")
        assert data["model_reasoning_effort"] in ("high", "low")
        # The prompt body survives verbatim through TOML's literal string.
        assert data["developer_instructions"].strip() == role["body"].strip()


def test_readme_invariants_are_verbatim_from_agents_md():
    gen = _load_generator()
    invariants = gen.extract_section((REPO / "AGENTS.md").read_text(), "Invariants")
    assert invariants, "could not extract Invariants from AGENTS.md"
    readme = (REPO / ".cursor" / "agents" / "README.md").read_text()
    assert invariants in readme, "fleet README invariants drifted from AGENTS.md"


# ---- the Cursor plugin (GRPH-364) -------------------------------------------------------

def _plugin(name):
    gen = _load_generator()
    return gen.render_files()[f"{gen.PLUGIN_DIR}/{name}"]


def test_the_plugin_ships_no_mcp_config_at_all():
    """It cannot. Credentials are per-install, and Cursor does not interpolate them from the
    environment — probed against 3.16.2 with the variables present: `${env:VAR}`, `${VAR}` and
    `$VAR` are all ignored and the entry is silently DROPPED rather than sent as a literal.

    So a committed `mcp.json` here could only be a placeholder every user hand-edits — a
    generated file that must be modified, which is precisely what `--check` exists to prevent.
    The Fleet view emits the config instead, because it has the keys at mint time."""
    gen = _load_generator()
    files = gen.render_files()

    assert not any(f.endswith("mcp.json") for f in files), \
        "a shipped mcp.json is either credential-bearing or broken"


def test_no_orphan_survives_in_the_plugin_directory():
    """`--check` verifies that every generated file is current; nothing noticed a file that
    STOPPED being generated. The broken `mcp.json` sat here after being dropped from the
    generator, and a stale config is worse than a missing one — it silently connects nothing."""
    gen = _load_generator()
    expected = {f.split("/")[-1] for f in gen.render_files() if f.startswith(gen.PLUGIN_DIR)}
    on_disk = {p.name for p in (REPO / gen.PLUGIN_DIR).iterdir() if p.is_file()}

    assert on_disk <= expected, f"orphaned generated file(s): {sorted(on_disk - expected)}"


def test_the_plugin_ships_the_fleet_agents_so_installing_it_installs_the_fleet():
    """A plugin that configured credentials but shipped no agents would leave the user to find
    the role prompts separately — which is most of the setup cost this exists to remove."""
    gen = _load_generator()
    files = gen.render_files()

    for role in gen.FLEET_ROSTER:
        assert f"{gen.PLUGIN_DIR}/agents/{role['name']}.md" in files


def test_the_plugin_readme_states_what_it_does_not_guarantee():
    """The guarantee got STRONGER with enrolment, and the caveat had to move rather than go.

    Before seats, the config named three servers with three keys, and the honest caveat was
    that Cursor cannot scope a server to one agent — so an agent could switch servers and sign
    its own work. There is now ONE server and one credential, so that risk does not exist: the
    role comes from a seat the SERVER issued, and a worker cannot promote itself because it has
    nothing to promote itself with.

    What remains is smaller and still real: an agent handed two codes can use either. Saying so
    is the point — this repo's recurring defect is a claim stronger than what is enforced, and
    a README that stopped naming any limit would be exactly that."""
    readme = _plugin("README.md")

    assert "not an adversarial boundary" in readme
    assert "two codes" in readme, "name the residual risk, not a retired one"
    assert "cannot scope" not in readme, "that caveat described the three-server config"


# ---- the Cursor rules file (GRPH-147) -----------------------------------------------------

RULE = ".cursor/rules/agentledger.mdc"


def test_the_rule_is_mdc_because_cursor_ignores_md_here():
    """THE FAILURE THIS FILE MOST HAS TO AVOID, and it is silent.

    Cursor's rules system reads `.mdc` files in `.cursor/rules` and **ignores plain `.md`**
    there. A rule committed as `.md` looks installed, reviews fine, sits in the right
    directory, and does nothing at all — there is no error and no warning, so the only
    symptom is agents continuing to behave exactly as they did before.

    Asserted on the generator's own output rather than on disk alone, so renaming the
    committed file is not enough to make this pass.
    """
    files = _load_generator().render_files()

    assert RULE in files, "the generator no longer emits the Cursor rule"
    assert not any(p.startswith(".cursor/rules/") and p.endswith(".md") for p in files), (
        "a rule is being emitted as .md in .cursor/rules — Cursor ignores those entirely, "
        "so it would ship as a no-op"
    )
    assert (REPO / RULE).exists(), f"{RULE} missing — run scripts/gen_subagents.py"


def test_the_rule_applies_unconditionally():
    """`alwaysApply: true` is the only frontmatter combination that fits this rule.

    Cursor's rule anatomy: `true` includes it always; `false` + `globs` auto-attaches on
    matching files; `false` + `description` lets the agent pull it in when it judges it
    relevant; neither means it arrives only on an explicit @-mention. A rule whose first
    instruction is *claim the item before you start* cannot wait for a glob to match — by
    the time a file is open, the agent has already started.
    """
    body = _load_generator().render_files()[RULE]
    head = body.split("---")[1]

    assert "alwaysApply: true" in head, (
        "the ledger-loop rule is no longer applied unconditionally; if that is deliberate, "
        "note that globs attach only after work has already begun"
    )
    assert "description:" in head, "no description — a human reading the rules list sees nothing"


def test_the_rule_is_the_agents_md_section_verbatim():
    """Generated, not written, so it cannot drift. The item asked for a thin pointer to
    AGENTS.md rather than a second copy of the guidance, and a paraphrase here is exactly
    the drift that GRPH-424 and GRPH-528 were both filed for."""
    gen = _load_generator()
    loop = gen.extract_section((REPO / "AGENTS.md").read_text(), "Ledger loop")

    assert loop, "AGENTS.md lost its '## Ledger loop' section — the rule has no source"
    assert loop in gen.render_files()[RULE], \
        "the rule's body is no longer AGENTS.md's Ledger loop verbatim"


def test_the_rule_points_at_agents_md_instead_of_restating_it():
    """It rides on every request, so its length is a running cost — the same reason the MCP
    manifest has a token budget. It carries the loop and defers the rest."""
    body = _load_generator().render_files()[RULE]

    # The PROSE pointer, not the bare string. `AGENTS.md` also appears in the generated-by
    # comment at the top of every rendering, so `"AGENTS.md" in body` is true even when the
    # sentence telling a reader where to go has been deleted — it passes for the wrong
    # reason. Same trap as GRPH-524: anchor on the phrase that carries the meaning.
    pointer = "is in `AGENTS.md` at the repo root"
    assert pointer in body, (
        "the rule no longer tells a reader where the rest of the guidance lives; the "
        "generated-by comment mentions AGENTS.md but is not an instruction"
    )
    invariants = _load_generator().extract_section((REPO / "AGENTS.md").read_text(), "Invariants")
    first_invariant = invariants.strip().splitlines()[0].strip()
    assert first_invariant not in body, (
        "the rule has started restating AGENTS.md's invariants; two copies drift and this "
        "one is paid for on every request"
    )
    assert len(body) < 4000, f"the always-applied rule has grown to {len(body)} chars"
