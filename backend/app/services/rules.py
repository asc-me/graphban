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

from app.services import gitops
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
    Rule("gitops.base_branch", "branching", "Base branch",
         "Which branch work is cut from and merged back to.",
         "advisory", _GITOPS_CONTEXT, "db", channel="context", gitops_field="base_branch"),
    Rule("gitops.no_push_to_base", "branching", "No push to base",
         "Agents open a pull request instead of pushing to the base branch.",
         "advisory", _GITOPS_CONTEXT, "db", channel="context", gitops_field="no_push_to_base",
         models=("prs_to_base", "prs_to_integration")),
    Rule("gitops.branch_name_pattern", "branching", "Branch naming",
         "Branches follow the project's naming pattern.",
         "advisory", _GITOPS_CONTEXT, "db", channel="context",
         gitops_field="branch_name_pattern"),
    Rule("gitops.pr_title_pattern", "pull_request", "PR title pattern",
         "Pull request titles follow the project's pattern.",
         "advisory", _GITOPS_CONTEXT, "db", channel="context", gitops_field="pr_title_pattern",
         models=("prs_to_base", "prs_to_integration")),
    Rule("gitops.reviewer_bar", "review", "Reviewer bar",
         "What counts as a review: a Graphban sign-off, a forge approval, or both.",
         "advisory", _GITOPS_CONTEXT, "db", channel="context", gitops_field="reviewer_bar"),
    Rule("gitops.model", "branching", "Gitops model",
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
    Rule("gate.approval_earned_by_grill", "approval", "Approval is earned by the grill",
         "A PRD becomes approved by finishing its grill, never by setting the field.",
         "enforced", _src("app/services/prds.py", "ApprovalNotEarned"), "code"),
    Rule("gate.decompose_requires_approval", "approval", "Decompose needs approval",
         "Tasks are filed from a PRD only once it has earned approval.",
         "enforced", _src("app/services/prds.py", "DecomposeRequiresApproval"), "code"),
    Rule("gitops.observe_answer_required", "build", "Observe before the plan",
         "A gitops plan's Observe step needs a named answer before it, or the plan, can move on.",
         "enforced", _src("app/services/gitops.py", "refuse_observe_progress"), "code"),
    Rule("gitops.checklist_needs_evidence", "build", "Checklist children need evidence",
         "A gitops checklist child cannot reach review or done without evidence.",
         "enforced", _src("app/services/gitops.py", "refuse_already_progress"), "code"),
    Rule("gitops.owned_by_org", "branching", "Gitops owned by the org",
         "On a linked instance the git process is set by the org admin; project edits are refused.",
         "enforced", _src("app/routers/projects.py", "Refused"), "code", scope="org"),
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
         "enforced", _src("app/services/memory.py", "agent_publish"), "code"),
    Rule("memory.write_access", "learn", "Changing memory needs write access",
         "Retiring, restoring or applying recommendations to a shard needs write access to its "
         "project, or to at least one project when the shard has none.",
         "enforced", _src("app/routers/memory.py", "refuse_memory_write"), "code"),
    Rule("memory.retire_window", "learn", "Retire and restore follow the shard's state",
         "Only a published shard can be retired, and a retired shard can be restored for "
         "14 days; after that the retirement is final.",
         "enforced", _src("app/services/memory.py", "RetireRefused"), "code"),
    Rule("review.role_gated_tools", "review", "Role-gated tools",
         "MCP tools outside an agent's role are refused.",
         "enforced", _src("app/services/fleet.py", "check_tool_role"), "code"),
    Rule("review.reviewer_diversity", "review", "Reviewer diversity",
         "Review should come from a different vendor than the builder.",
         "advisory", _src("app/services/fleet.py"), "code", channel="doc"),
    Rule("ci.backend_suite_both_engines", "merge", "Backend suite on both engines",
         "CI runs the backend tests on SQLite and Postgres.",
         "checked", Enforcer("ci_job", ".github/workflows/ci.yml"), "repo_file"),
    Rule("forge.branch_protection", "merge", "Forge branch protection",
         "The forge refuses direct pushes to the base branch.",
         "external", Enforcer("forge", "branch protection"), "code"),
    Rule("budget.cap", "build", "Budget cap",
         "The gbfleet supervisor stops a worker that exceeds its budget.",
         "external", Enforcer("supervisor", "gbfleet budget cap"), "code", scope="org"),
    Rule("agents.operating_loop", "build", "Operating loop",
         "Agents follow the operating loop and invariants in AGENTS.md.",
         "advisory", Enforcer("doc", "AGENTS.md"), "repo_file", channel="doc"),
)

RULES_BY_ID = {r.id: r for r in RULES}

#: Written only by a passing build on main (`scripts/refusal_ratchet.py publish`, GRPH-1158).
REFUSAL_COVERAGE_FILE = Path(__file__).resolve().parents[1] / "data" / "refusal_coverage.json"


def refusal_coverage(path: Path | None = None) -> dict:
    """"N of M refusal sites name their rule", as the last green main build published it.

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
    return f"{REPO_URL}/blob/v{version or __version__}/backend/{path}"


def _enforcer_out(e: Enforcer) -> dict:
    url = source_url(e.ref) if e.kind == "graphban_source" else ""
    return {"kind": e.kind, "ref": e.ref, "url": url}


def _raise_names(node: ast.Raise) -> set[str]:
    exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
    if isinstance(exc, ast.Name):
        return {exc.id}
    if isinstance(exc, ast.Attribute):
        return {exc.attr}
    return set()


def raises_symbol(source: str, symbol: str) -> bool:
    """True when `symbol` is a refusal in `source`: `raise symbol(...)`, or a function named
    `symbol` whose body raises. A name that only appears in a comment or string is not one."""
    if not symbol:
        return False
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Raise) and node.exc is not None and symbol in _raise_names(node):
            return True
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol
                and any(isinstance(n, ast.Raise) for n in ast.walk(node))):
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


def registry(db: Session, project_id: str | None) -> list[dict]:
    view = gitops.resolve(db, project_id)
    out = []
    for r in RULES:
        d = {
            "id": r.id, "stage": r.stage, "title": r.title, "summary": r.summary,
            "enforcement": r.enforcement, "channel": r.channel,
            "enforcer": _enforcer_out(r.enforcer), "truth": r.truth, "scope": r.scope,
            "models": list(r.models), "value": None, "source": None,
        }
        if r.truth == "db":
            v = _db_value(r, view)
            d["value"], d["source"] = v["value"], v["source"]
            # Setting a gitops value moves unmeasured -> advisory, never to enforced.
            if v["source"] == "unmeasured" and r.gitops_field:
                d["enforcement"], d["channel"] = "unmeasured", None
        out.append(d)
    return out


# --- Flow model (GRPH-1156 / PRD-P61 §4) -------------------------------------------------
# The nodes are fixed in v1 and so are the edges: the flow is always drawn whole. The
# registry's own `stage` is finer-grained than the map, so each rule is placed explicitly.

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

RULE_STAGE = {
    "gate.approval_earned_by_grill": "spec",
    "gate.decompose_requires_approval": "plan",
    "gitops.branch_name_pattern": "build",
    "gitops.observe_answer_required": "build",
    "gitops.checklist_needs_evidence": "build",
    "gitops.owned_by_org": "build",
    "reach.deploy_not_delegable": "plan",
    "reach.acknowledge_signals": "plan",
    "budget.cap": "build",
    "agents.operating_loop": "build",
    "review.not_own_work": "review",
    "review.role_gated_tools": "review",
    "review.adversarial_evidence": "review",
    "review.attest_the_head": "review",
    "review.predicates_compared": "review",
    "review.clause_capability": "review",
    "review.acceptance_covered": "review",
    "memory.independent_adjudication": "learn",
    "memory.write_access": "learn",
    "memory.retire_window": "learn",
    "review.reviewer_diversity": "review",
    "gitops.reviewer_bar": "review",
    "gitops.base_branch": "merge",
    "gitops.no_push_to_base": "merge",
    "gitops.model": "merge",
    "gitops.pr_title_pattern": "merge",
    "ci.backend_suite_both_engines": "merge",
    "forge.branch_protection": "merge",
    "release.version_from": "release",
    "release.release_defined_in": "release",
}

NOT_USED_LABEL = "not used by this project's model"

#: Rules that apply to a model only in part, beyond the registry's `models` field.
_PR_STEP_MODELS = ("push_to_base",)


def shipped_skills() -> list[str] | None:
    """Skills the `gban` package ships at the running version. None when the package is not
    installed here: the server cannot read a project's `.claude/skills`, and a guessed list
    would read as a measured one."""
    try:
        from importlib import resources
        root = resources.files("gban") / "skills"
        return sorted(p.name for p in root.iterdir() if p.is_dir())
    except (ImportError, FileNotFoundError, NotADirectoryError, ModuleNotFoundError):
        return None


def rule_used(rule: dict, model: str | None) -> bool:
    """Per rule, from the registry's `models`: an unknown model (unmeasured) uses everything."""
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


def flow(db: Session, project_id: str | None) -> dict:
    """The project's process as stages in fixed order. Unused rules stay on their stage,
    flagged; a stage is `not_used` only when every rule on it is, and a stage with no rules
    registered is `unmeasured` — nothing was looked at, which is not the same as clean."""
    rules = registry(db, project_id)
    model = gitops.resolve(db, project_id).model.value
    nodes = [dict(r, stage=RULE_STAGE[r["id"]]) for r in rules]
    skills = shipped_skills()
    nodes.append({
        "id": "agents.skills_pack", "stage": "build",
        "title": "Agent instructions (Graphban pack)",
        "summary": "Skills the agents read, shipped in the gban package.",
        "enforcement": "advisory", "channel": "skill", "truth": "repo_file",
        "scope": "platform", "models": list(GITOPS_ALL_MODELS), "value": None,
        "source": None, "enforcer": {"kind": "skill", "ref": "gban skills", "url": ""},
        "skills": skills,
    })
    stages = []
    for sid, title, summary in STAGES:
        on = []
        for n in nodes:
            if n["stage"] != sid:
                continue
            used = rule_used(n, model)
            on.append(dict(n, used=used, label=None if used else NOT_USED_LABEL))
        used_rules = [n for n in on if n["used"]]
        if not on:
            state = "unmeasured"
        elif not used_rules:
            state = "not_used"
        else:
            state = weakest([n["enforcement"] for n in used_rules])
        stages.append({"id": sid, "title": title, "summary": summary, "state": state,
                       "rules": on})
    return {"model": model, "stages": stages, "edges": [list(e) for e in EDGES]}
