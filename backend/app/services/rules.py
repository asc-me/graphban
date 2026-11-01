"""Rule registry (GRPH-1155 / PRD-P61 §3): every delivery rule, declared as data.

Describes rules that code already enforces; it creates no enforcement. A drawn rule must
never read as an enforced one, so `enforced` is reserved for a rule with a server refusal
path, and `tests/test_rules_registry.py` fails the PR that moves an enforcer without moving
its entry here.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Item
from app.services import gitops, items
from app.version import __version__

ENFORCEMENTS = ("enforced", "checked", "external", "advisory", "unmeasured")
ENFORCER_KINDS = (
    "graphban_source", "ci_job", "scan", "skill", "doc", "context", "forge", "supervisor",
)
TRUTHS = ("db", "code", "repo_file", "derived")
SCOPES = ("project", "org", "platform")
CHANNELS = ("context", "skill", "doc")
GITOPS_ALL_MODELS = gitops.GITOPS_MODELS

REPO_URL = "https://github.com/asc-me/graphban"
BACKEND_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Enforcer:
    kind: str
    ref: str
    #: For `graphban_source`: the refusal — an exception class the file raises, or a function
    #: in it that raises (tested by `raises_symbol`, not by a substring match).
    symbol: str = ""


@dataclass(frozen=True)
class Rule:
    id: str
    #: One of `STAGE_IDS` — the node of the flow (PRD-P61 §4) the rule governs.
    stage: str
    title: str
    summary: str
    enforcement: str
    enforcer: Enforcer
    truth: str
    scope: str = "project"
    channel: str | None = None
    models: tuple[str, ...] = GITOPS_ALL_MODELS
    gitops_field: str | None = None
    extra: dict = field(default_factory=dict)


def _src(path: str, symbol: str = "") -> Enforcer:
    return Enforcer("graphban_source", path, symbol)


_GITOPS_CONTEXT = Enforcer("context", "get_context.gitops")

_GITOPS_RULES = (
    Rule("gitops.base_branch", "merge", "Base branch",
         "Which branch work is cut from and merged back to.",
         "advisory", _GITOPS_CONTEXT, "db", channel="context", gitops_field="base_branch"),
    Rule("gitops.no_push_to_base", "merge", "No push to base",
         "Agents open a pull request instead of pushing to the base branch.",
         "advisory", _GITOPS_CONTEXT, "db", channel="context", gitops_field="no_push_to_base",
         models=("prs_to_base", "prs_to_integration")),
    Rule("gitops.branch_name_pattern", "build", "Branch naming",
         "Branches follow the project's naming pattern.",
         "advisory", _GITOPS_CONTEXT, "db", channel="context",
         gitops_field="branch_name_pattern"),
    Rule("gitops.pr_title_pattern", "merge", "PR title pattern",
         "Pull request titles follow the project's pattern.",
         "advisory", _GITOPS_CONTEXT, "db", channel="context", gitops_field="pr_title_pattern",
         models=("prs_to_base", "prs_to_integration")),
    Rule("gitops.reviewer_bar", "review", "Reviewer bar",
         "What counts as a review: a Graphban sign-off, a forge approval, or both.",
         "advisory", _GITOPS_CONTEXT, "db", channel="context", gitops_field="reviewer_bar"),
    Rule("gitops.model", "merge", "Gitops model",
         "The branching model the project runs (which of the gitops presets).",
         "advisory", _GITOPS_CONTEXT, "db", channel="context", gitops_field="model"),
    Rule("release.version_from", "release", "Version source",
         "Where the release version comes from (tag, semver or calver).",
         "advisory", _GITOPS_CONTEXT, "db", channel="context", gitops_field="version_from"),
    Rule("release.release_defined_in", "release", "Release process location",
         "The single path or URL where the project's release process is written.",
         "advisory", _GITOPS_CONTEXT, "db", channel="context",
         gitops_field="release_defined_in"),
)

RULES: tuple[Rule, ...] = _GITOPS_RULES + (
    Rule("gate.approval_earned_by_grill", "spec", "Approval is earned by the grill",
         "A PRD becomes approved by finishing its grill, never by setting the field.",
         "enforced", _src("app/services/prds.py", "ApprovalNotEarned"), "code"),
    Rule("gate.decompose_requires_approval", "plan", "Decompose needs approval",
         "Tasks are filed from a PRD only once it has earned approval.",
         "enforced", _src("app/services/prds.py", "DecomposeRequiresApproval"), "code"),
    Rule("gitops.observe_answer_required", "build", "Observe before the plan",
         "A gitops plan's Observe step needs a named answer before it, or the plan, can move on.",
         "enforced", _src("app/services/gitops.py", "refuse_observe_progress"), "code"),
    Rule("gitops.checklist_needs_evidence", "build", "Checklist children need evidence",
         "A gitops checklist child cannot reach review or done without evidence.",
         "enforced", _src("app/services/gitops.py", "refuse_already_progress"), "code"),
    Rule("gitops.owned_by_org", "build", "Gitops owned by the org",
         "On a linked instance the git process is set by the org admin; project edits are refused.",
         "enforced", _src("app/routers/projects.py", "Refused"), "code", scope="org"),
    Rule("sync.link_needs_box_admin", "build", "Linking the box needs a box admin",
         "Linking, re-pointing or unlinking this deployment's cloud org is done by the box's "
         "operator or an owner/admin of one of their projects, not by any member.",
         "enforced", _src("app/routers/sync.py", "refuse_unless_box_admin"), "code", scope="org"),
    Rule("reach.deploy_not_delegable", "plan", "Deploy-reach work is not delegated",
         "An item declared reach=deploy acts on a running system and is never delegated.",
         "enforced", _src("app/services/delegation.py", "Refused"), "code"),
    Rule("reach.acknowledge_signals", "plan", "Reach signals need an acknowledgement",
         "Work whose text suggests a running system needs acknowledge_reach=true to delegate.",
         "enforced", _src("app/services/delegation.py", "Refused"), "code"),
    Rule("review.not_own_work", "review", "Not your own work",
         "The agent that built an item cannot sign it off.",
         "enforced", _src("app/services/fleet.py", "SelfReview"), "code"),
    Rule("review.adversarial_evidence", "review", "Sign-off needs adversarial evidence",
         "Work above the effort threshold is signed off only with a sabotage receipt.",
         "enforced", _src("app/services/fleet.py", "MissingAdversarialEvidence"), "code"),
    Rule("review.attest_the_head", "review", "Attest the head, not the base",
         "A sign-off names the commit reviewed, never the commit the branch was cut from.",
         "enforced", _src("app/services/fleet.py", "AttestedTheBase"), "code"),
    Rule("review.predicates_compared", "review", "Predicates must have run",
         "A check that could not run is not a pass; it needs a comparison or a recorded waiver.",
         "enforced", _src("app/services/fleet.py", "UncomparedPredicate"), "code"),
    Rule("review.clause_capability", "review", "Reviewer holds the clause's capability",
         "An acceptance clause needing a tool or a human is signed off only by a reviewer "
         "who declared it.",
         "enforced", _src("app/services/fleet.py", "ClauseCapability"), "code"),
    Rule("review.acceptance_covered", "review", "Every acceptance clause is tested",
         "Sign-off names a test for each clause under ## Acceptance.",
         "enforced", _src("app/services/fleet.py", "MissingAcceptanceCoverage"), "code"),
    Rule("memory.independent_adjudication", "learn", "Agents do not publish their own memory",
         "An agent may discard its candidate shard; only an independent judge publishes one.",
         "advisory", _src("app/services/memory.py", "agent_publish"), "code", channel="doc"),
    Rule("memory.write_access", "learn", "Changing memory needs write access",
         "Retiring, restoring or applying recommendations to a shard needs write access to its "
         "project, or to at least one project when the shard has none.",
         "enforced", _src("app/routers/memory.py", "refuse_memory_write"), "code"),
    Rule("memory.retire_window", "learn", "Retire and restore follow the shard's state",
         "Only a published shard can be retired or kept, and a retired shard can be restored for "
         "14 days; after that the retirement is final.",
         "enforced", _src("app/services/memory.py", "RetireRefused"), "code"),
    Rule("review.role_gated_tools", "review", "Role-gated tools",
         "MCP tools outside an agent's role are refused.",
         "enforced", _src("app/services/fleet.py", "check_tool_role"), "code"),
    Rule("review.reviewer_diversity", "review", "Reviewer diversity",
         "Review should come from a different vendor than the builder.",
         "advisory", _src("app/services/fleet.py"), "code", channel="doc"),
    Rule("merge.ci_green", "merge", "Merged work passed CI",
         "CI attests suite_green on each item's branch; this reads the latest attestation on a "
         "merged item.",
         "checked", Enforcer("ci_job", ".github/workflows/ci.yml"), "derived"),
    Rule("forge.branch_protection", "merge", "Forge branch protection",
         "The forge refuses direct pushes to the base branch.",
         "external", Enforcer("forge", "branch protection"), "derived"),
    Rule("forge.required_checks", "merge", "Forge required checks",
         "Which checks the forge requires before a pull request can merge.",
         "external", Enforcer("forge", "required status checks"), "derived"),
    Rule("plan.budget_cap", "plan", "Budget cap",
         "The gbfleet supervisor stops a worker that exceeds its budget.",
         "external", Enforcer("supervisor", "gbfleet budget cap"), "db", scope="org"),
    Rule("rules.person_adopts", "learn", "A person adopts a rule",
         "Only a person's request turns a library rule on or off or changes its params; "
         "agents and the recommendation engine only suggest.",
         "enforced", _src("app/services/rule_toggles.py", "Refused"), "code"),
    Rule("rules.org_lock_holds", "learn", "An org lock holds",
         "A project cannot override a library rule its org has locked.",
         "enforced", _src("app/services/rule_toggles.py", "Refused"), "code", scope="org"),
    Rule("rules.toggle_needs_admin", "learn", "Toggles need a project admin",
         "A project's library rule toggles are set by its owner or an admin.",
         "enforced", _src("app/services/rule_toggles.py", "Refused"), "code"),
    Rule("agents.operating_loop", "build", "Operating loop",
         "Agents follow the operating loop and invariants in AGENTS.md.",
         "advisory", Enforcer("doc", "AGENTS.md"), "repo_file", channel="doc"),
)

RULES_BY_ID = {r.id: r for r in RULES}

#: Package data written by the build of what ships — `graphban_pack.py` runs
#: `scripts/refusal_ratchet.py publish`, which writes nothing for a failing tree (GRPH-1185).
#: Gitignored: a source tree or editable install has none and reports unpublished.
REFUSAL_COVERAGE_FILE = Path(__file__).resolve().parents[1] / "data" / "refusal_coverage.json"


def refusal_coverage(path: Path | None = None) -> dict:
    """"N of M refusal sites name their rule", as the build of this package measured it.

    Read, never computed: a number worked out at runtime would come from whatever tree is
    deployed, failing or not. No file is `published: False` — nobody measured — and never a
    zero, which would read as "no refusal names its rule"."""
    import json

    try:
        data = json.loads((path or REFUSAL_COVERAGE_FILE).read_text())
    except (FileNotFoundError, ValueError):
        return {"published": False, "n": None, "m": None, "commit": None,
                "published_at": None}
    return {"published": True, "n": data["n"], "m": data["m"],
            "commit": data.get("commit") or None, "published_at": data.get("published_at")}


def source_url(path: str, version: str | None = None) -> str:
    """Graphban's own repo at the running release tag — not the tenant's code graph."""
    # The tag is the bare CalVer (`2026.10.24`) — scripts/graphban_release.py publishes no `v`.
    return f"{REPO_URL}/blob/{version or __version__}/backend/{path}"


def repo_file_url(path: str, version: str | None = None) -> str:
    """A repo-root file (ci.yml, AGENTS.md, docs/…) at the running release tag."""
    return f"{REPO_URL}/blob/{version or __version__}/{path}"


def _enforcer_out(e: Enforcer) -> dict:
    if e.kind == "graphban_source":
        url = source_url(e.ref)
    elif e.kind in ("ci_job", "doc"):
        url = repo_file_url(e.ref)
    else:
        url = ""
    return {"kind": e.kind, "ref": e.ref, "url": url}


#: Exception classes that ARE a refusal by policy. A function enforcer counts only when it
#: raises one of these — a bare `raise` of anything else (an `Unavailable`, a `ValueError`)
#: is an error path, and reporting it as enforcement would be a drawn rule read as a held one.
REFUSAL_CLASSES = ("Refused", "Forbidden")
#: The refusal class that carries `rule_id=`; the others cannot name a rule.
RULE_NAMING_CLASSES = ("Refused",)


def _raise_names(node: ast.Raise) -> set[str]:
    exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
    if isinstance(exc, ast.Name):
        return {exc.id}
    if isinstance(exc, ast.Attribute):
        return {exc.attr}
    return set()


def _names_rule(node: ast.Raise, rule_id: str, consts: dict[str, str]) -> bool:
    """`raise Refused(..., rule_id="x")` — the raise carries the rule it enforces. The id may
    be a module-level string constant (`rule_id=OBSERVE_RULE`)."""
    call = node.exc
    if not isinstance(call, ast.Call):
        return False
    for k in call.keywords:
        if k.arg != "rule_id":
            continue
        v = k.value
        if isinstance(v, ast.Constant) and v.value == rule_id:
            return True
        if isinstance(v, ast.Name) and consts.get(v.id) == rule_id:
            return True
    return False


def raises_symbol(source: str, symbol: str, rule_id: str | None = None) -> bool:
    """True when `symbol` is a refusal in `source`: `raise symbol(...)`, or a function named
    `symbol` whose body raises a refusal class (`REFUSAL_CLASSES`). A name that only appears
    in a comment or string is not one, and neither is a function raising something else.

    With `rule_id`, a shared refusal class (`Refused`) counts only at a raise that names that
    rule, so one rule's raise cannot vouch for another's."""
    if not symbol:
        return False
    tree = ast.parse(source)
    consts = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
              and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
              for t in n.targets if isinstance(t, ast.Name)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and node.exc is not None and symbol in _raise_names(node):
            if (rule_id is None or symbol not in RULE_NAMING_CLASSES
                    or _names_rule(node, rule_id, consts)):
                return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol:
            for n in ast.walk(node):
                if (isinstance(n, ast.Raise) and n.exc is not None
                        and _raise_names(n) & set(REFUSAL_CLASSES)
                        and (rule_id is None or not _raise_names(n) & set(RULE_NAMING_CLASSES)
                             or _names_rule(n, rule_id, consts))):
                    return True
    return False


def gitops_view_fields() -> set[str]:
    """Every value the gitops view resolves, read from its schema — a field added there
    without a registry entry fails `test_gitops_rules_cover_the_view`."""
    from app.schemas import GitopsField, GitopsFields, GitopsView
    top = {n for n, f in GitopsView.model_fields.items() if f.annotation is GitopsField}
    return top | set(GitopsFields.model_fields)


def _db_value(rule: Rule, view) -> dict:
    if rule.gitops_field is None:
        return {"value": None, "source": "unmeasured"}
    holder = view.fields if rule.gitops_field in gitops.FIELDS else view
    f = getattr(holder, rule.gitops_field)
    return {"value": f.value, "source": f.source}


BUDGET_CAP = "plan.budget_cap"


def _budget_cap_value(db: Session, project_id: str | None) -> dict:
    from app.services import fleet_profiles
    policy = fleet_profiles.policy_of(db, project_id)
    cap = ((policy or {}).get("caps") or {}).get("per_item_tokens")
    if cap is None:
        return {"value": None, "source": "unmeasured"}
    return {"value": str(cap), "source": "project"}


def registry(db: Session, project_id: str | None, view=None) -> list[dict]:
    """`view` is a gitops view the caller already resolved: one fetch per request, because on
    a linked box each resolve is an org fetch and two of them can disagree (GRPH-1196)."""
    if view is None:
        view = gitops.resolve(db, project_id)
    out = []
    for r in RULES:
        d = {
            "id": r.id, "stage": r.stage, "title": r.title, "summary": r.summary,
            "enforcement": r.enforcement, "channel": r.channel,
            "enforcer": _enforcer_out(r.enforcer), "truth": r.truth, "scope": r.scope,
            "models": list(r.models), "value": None, "source": None, "control": None,
        }
        if r.truth == "db":
            v = _budget_cap_value(db, project_id) if r.id == BUDGET_CAP else _db_value(r, view)
            d["value"], d["source"] = v["value"], v["source"]
            # Setting a gitops value moves unmeasured -> advisory, never to enforced.
            if v["source"] == "unmeasured" and r.gitops_field and r.id != BUDGET_CAP:
                d["enforcement"], d["channel"] = "unmeasured", None
        out.append(d)
    return out + _library_entries(db, project_id)


def _library_entries(db: Session, project_id: str | None) -> list[dict]:
    """Library rules (GRPH-1262 / PRD-P63 D3): `truth = db` for the state and params, which
    resolve project → org → platform default, and `control = toggle`. Drawn at the level the
    PRIMITIVE provides; a rule that is off stays in the list and `rule_used` draws it not used."""
    from app.services import rule_toggles
    out = []
    for t in rule_toggles.effective(db, project_id):
        lib = rule_toggles.LIBRARY[t["rule_id"]]
        out.append({
            "id": lib.id, "stage": lib.stage, "title": lib.title, "summary": lib.summary,
            "enforcement": lib.enforcement, "channel": None,
            "enforcer": _enforcer_out(lib.enforcer), "truth": "db", "scope": "project",
            "models": list(GITOPS_ALL_MODELS), "control": "toggle",
            "value": t["on"]["value"], "source": t["on"]["source"],
            "params": t["params"], "locked": t["locked"],
            **({"unmeasured_reason": lib.unmeasured_reason} if lib.unmeasured_reason else {}),
        })
    return out


# --- Flow model (GRPH-1156 / PRD-P61 §4) -------------------------------------------------
# The nodes are fixed in v1 and so are the edges: the flow is always drawn whole. The
# each rule carries its own stage id, so the registry and the map share one vocabulary.

STAGES = (
    ("spec", "Spec", "PRD, grill, approval"),
    ("plan", "Plan", "Decompose, allocate, seat"),
    ("build", "Build", "Claim, branch, naming"),
    ("review", "Review", "Authorship, diversity, reviewer bar, sign-off"),
    ("merge", "Merge", "Base branch, push rules, CI"),
    ("release", "Release", "Version source, release definition"),
    ("learn", "Learn", "Scans, lessons"),
)
STAGE_IDS = tuple(s[0] for s in STAGES)
EDGES = tuple((a, b) for a, b in zip(STAGE_IDS, STAGE_IDS[1:]))

#: strongest first; a stage takes the weakest of its used rules.
ENFORCEMENT_ORDER = ("enforced", "checked", "external", "advisory", "unmeasured")

NOT_USED_LABEL = "not used by this project's model"
#: A library rule whose toggle is off: drawn, never hidden (PRD-P63 D3).
TOGGLED_OFF_LABEL = "turned off for this project"

#: Rules that apply to a model only in part, beyond the registry's `models` field.
_PR_STEP_MODELS = ("push_to_base",)


#: Package data written by the build (`graphban_pack.py`): the skill names and the gban version
#: they ship at. Gitignored like the coverage file; a dev checkout reads the repo tree instead.
GBAN_SKILLS_FILE = Path(__file__).resolve().parents[1] / "data" / "gban_skills.json"
_CLI_ROOT = Path(__file__).resolve().parents[3] / "cli"


def shipped_skills(path: Path | None = None, cli_root: Path | None = None) -> dict | None:
    """The skills the `gban` package ships and its version: `{version, skills: [{name, url}]}`,
    each url at `cli-v<version>`. Read from package data, else from a repo checkout's
    `cli/` (pyproject.toml + src/gban/skills). None when neither exists: the server cannot read
    a project's `.claude/skills`, and a guessed list would read as a measured one."""
    import json
    import tomllib

    try:
        data = json.loads((path or GBAN_SKILLS_FILE).read_text())
        version, names = data["version"], data["skills"]
    except (FileNotFoundError, ValueError, KeyError):
        root = cli_root or _CLI_ROOT
        try:
            version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
            names = [p.name for p in (root / "src" / "gban" / "skills").iterdir() if p.is_dir()]
        except (FileNotFoundError, NotADirectoryError, KeyError, ValueError):
            return None
    base = f"{REPO_URL}/blob/cli-v{version}/cli/src/gban/skills"
    return {"version": version,
            "skills": [{"name": n, "url": f"{base}/{n}/SKILL.md"} for n in sorted(names)]}


def rule_used(rule: dict, model: str | None) -> bool:
    """Per rule, from the registry's `models`: an unknown model (unmeasured) uses everything."""
    if rule.get("control") == "toggle" and rule["value"] is not True:
        return False
    if model is None:
        return True
    if model not in rule["models"]:
        return False
    # The reviewer bar is a PR-step rule only when it asks the forge.
    if rule["id"] == "gitops.reviewer_bar" and model in _PR_STEP_MODELS:
        return rule["value"] != "forge"
    return True


def weakest(enforcements: list[str]) -> str:
    return max(enforcements, key=ENFORCEMENT_ORDER.index)


def _merged(item: Item) -> bool:
    """Done, with a PR that was linked: the work landed through a pull request."""
    if item.status != "done":
        return False
    return item.pr_linked_at is not None or any(
        isinstance(e, dict) and e.get("kind") == "url" and items.is_pr_url(e.get("url", ""))
        for e in item.evidence or [])


def ci_last_result(db: Session, project_id: str | None) -> dict | None:
    """The most recent `suite_green` attestation on a merged item of this project, or None.

    Read from stored receipts only — no live fetch. Receipts carry no clock of their own, so
    `at` is when the merged item last changed (its sign-off), the nearest date the server holds.
    A failing attestation is a result too: it reports `fail`, never silence."""
    if not project_id:
        return None
    best = None
    for it in db.query(Item).filter(Item.project_id == project_id, Item.status == "done"):
        if not _merged(it):
            continue
        runs = [a for a in items.attestation_receipts(it.evidence)
                if any(isinstance(q, dict) and q.get("name") == items.CI_PREDICATE
                       and q.get("compared") is not False for q in a.get("predicates") or [])]
        if not runs:
            continue
        last = runs[-1]
        passed = all(q.get("passed") is True for q in last["predicates"]
                     if q.get("name") == items.CI_PREDICATE)
        at = items._aware(it.updated_at)
        if best is None or at > best[0]:
            best = (at, {"outcome": "pass" if passed else "fail", "at": at.isoformat(),
                         "item": it.key, "commit": last.get("commit", "")})
    return best[1] if best else None


def flow(db: Session, project_id: str | None, user_id: str | None = None) -> dict:
    """The project's process as stages in fixed order. Unused rules stay on their stage,
    flagged; a stage is `not_used` only when every rule on it is, and a stage with no rules
    registered is `unmeasured` — nothing was looked at, which is not the same as clean."""
    view = gitops.resolve(db, project_id)
    if user_id is not None:
        gitops.fill_writable(view, db, user_id)
    rules = registry(db, project_id, view)
    model = view.model.value
    nodes = list(rules)
    for n in nodes:
        if n["enforcement"] == "checked":
            n["last_result"] = ci_last_result(db, project_id) if n["id"] == "merge.ci_green" else None
    shipped = shipped_skills()
    nodes.append({
        "id": "agents.skills_pack", "stage": "build",
        "title": "Agent instructions (Graphban pack)",
        "summary": "Skills the agents read, shipped in the gban package.",
        "enforcement": "advisory", "channel": "skill", "truth": "repo_file",
        "scope": "platform", "models": list(GITOPS_ALL_MODELS), "value": None,
        "source": None, "enforcer": {"kind": "skill", "ref": "gban skills", "url": ""},
        "skills": shipped["skills"] if shipped else None,
        "skills_version": shipped["version"] if shipped else None,
    })
    stages = []
    for sid, title, summary in STAGES:
        on = []
        for n in nodes:
            if n["stage"] != sid:
                continue
            used = rule_used(n, model)
            label = None if used else (
                TOGGLED_OFF_LABEL if n.get("control") == "toggle" and n["value"] is not True
                else NOT_USED_LABEL)
            on.append(dict(n, used=used, label=label))
        used_rules = [n for n in on if n["used"]]
        if not on:
            state = "unmeasured"
        elif not used_rules:
            state = "not_used"
        else:
            state = weakest([n["enforcement"] for n in used_rules])
        stages.append({"id": sid, "title": title, "summary": summary, "state": state,
                       "rules": on})
    return {"model": model, "stages": stages, "edges": [list(e) for e in EDGES],
            "control": _control(db, view)}


def _control(db: Session, view) -> dict:
    """Which plane holds the git process, so the view needs no second gitops fetch."""
    from app.services import code_sync
    org = (code_sync.link_status(db).get("org") if view.control.state != "local" else "") \
        or view.org_id or ""
    return {"state": view.control.state, "writable": view.control.writable,
            "message": view.control.message, "org_name": org}
