"""Rule registry (GRPH-1155 / PRD-P61 §3): every delivery rule, declared as data.

Describes rules that code already enforces; it creates no enforcement. A drawn rule must
never read as an enforced one, so `enforced` is reserved for a rule with a server refusal
path, and `tests/test_rules_registry.py` fails the PR that moves an enforcer without moving
its entry here.
"""
from __future__ import annotations

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
    #: For `graphban_source`: a name that must appear in the file's refusal path (tested).
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
)

RULES: tuple[Rule, ...] = _GITOPS_RULES + (
    Rule("gate.approval_earned_by_grill", "approval", "Approval is earned by the grill",
         "A PRD becomes approved by finishing its grill, never by setting the field.",
         "enforced", _src("app/services/prds.py", "ApprovalNotEarned"), "code"),
    Rule("gate.decompose_requires_approval", "approval", "Decompose needs approval",
         "Tasks are filed from a PRD only once it has earned approval.",
         "enforced", _src("app/services/prds.py", "DecomposeRequiresApproval"), "code"),
    Rule("review.not_own_work", "review", "Not your own work",
         "The agent that built an item cannot sign it off.",
         "enforced", _src("app/services/items.py", "built_by"), "code"),
    Rule("review.role_gated_tools", "review", "Role-gated tools",
         "MCP tools outside an agent's role are refused.",
         "enforced", _src("app/mcp_server.py", "role_refused"), "code"),
    Rule("review.reviewer_diversity", "review", "Reviewer diversity",
         "Review should come from a different vendor than the builder.",
         "advisory", _src("app/services/fleet.py"), "code", channel="doc"),
    Rule("release.version_from", "release", "Version source",
         "Where the release version comes from (tag, semver or calver).",
         "advisory", _GITOPS_CONTEXT, "db", channel="context"),
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


def source_url(path: str, version: str | None = None) -> str:
    """Graphban's own repo at the running release tag — not the tenant's code graph."""
    return f"{REPO_URL}/blob/v{version or __version__}/backend/{path}"


def _enforcer_out(e: Enforcer) -> dict:
    url = source_url(e.ref) if e.kind == "graphban_source" else ""
    return {"kind": e.kind, "ref": e.ref, "url": url}


def _db_value(rule: Rule, view) -> dict:
    if rule.gitops_field is None:
        return {"value": None, "source": "unmeasured"}
    f = getattr(view.fields, rule.gitops_field)
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
