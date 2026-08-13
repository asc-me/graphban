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


def test_the_plugin_declares_one_server_per_role_each_with_its_own_key():
    """The whole point. One server with one key is what Cursor already does, and it is what
    makes every review non-independent — author and reviewer sharing a credential and a host
    are not two opinions."""
    import json

    gen = _load_generator()
    servers = json.loads(_plugin("mcp.json"))["mcpServers"]

    assert set(servers) == {f"graphban-{r}" for r in gen.WAVE_ROLES}
    keys = [s["headers"]["X-API-Key"] for s in servers.values()]
    assert len(set(keys)) == len(keys), "two roles pointing at one credential defeats this"


def test_the_plugin_never_carries_a_credential():
    """It is committed, and it is meant to be shared. Every key is an env reference, which is
    also what makes the config write-once: only the values rotate per wave."""
    import json

    servers = json.loads(_plugin("mcp.json"))["mcpServers"]

    for name, s in servers.items():
        val = s["headers"]["X-API-Key"]
        assert val.startswith("${env:"), f"{name} inlines a key instead of referencing one"
        assert "gb_sk_" not in val and "al_sk_" not in val


def test_the_plugin_env_names_match_the_fleet_view():
    """THE drift that would waste an afternoon. The Fleet view emits the export block and this
    file consumes it; if a name changes on one side the credential is simply never read, and
    the failure presents as `unauthorized` — which reads as a bad key rather than a bad name.
    Two languages, no shared module, so the agreement is asserted rather than assumed."""
    gen = _load_generator()
    wave_ts = (REPO / "web" / "src" / "features" / "fleet" / "wave.ts").read_text()

    for role, env_name in gen.ROLE_ENV.items():
        assert env_name in wave_ts, f"{role}: {env_name} is not the name the Fleet view exports"
        assert f'"{env_name}"' in wave_ts or f"{env_name}" in wave_ts


def test_the_plugin_ships_the_fleet_agents_so_installing_it_installs_the_fleet():
    """A plugin that configured credentials but shipped no agents would leave the user to find
    the role prompts separately — which is most of the setup cost this exists to remove."""
    gen = _load_generator()
    files = gen.render_files()

    for role in gen.FLEET_ROSTER:
        assert f"{gen.PLUGIN_DIR}/agents/{role['name']}.md" in files


def test_the_plugin_readme_states_what_it_does_not_guarantee():
    """Cursor cannot scope a server to one agent, so an agent that switches servers can still
    sign its own work. Shipping the config without saying so would sell a boundary that is
    really a default — and this repo's recurring defect is exactly a claim stronger than what
    is enforced."""
    readme = _plugin("README.md")

    assert "cannot scope" in readme
    assert "not as an adversarial boundary" in readme
