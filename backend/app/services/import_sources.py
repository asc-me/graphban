"""Parsers for the structured sources of a rules import (GRPH-1949 / P92 D1).

Pure functions: file text (or forge JSON) in, findings out, in the shape
`import_runs.create_run` takes. No model reads anything here. A source a parser cannot read is
returned as *unread* with the reason, never as empty, so a run that could not see branch
protection is not reported clean. Proposals carry a `file` citation at the run's commit, so the
pipeline's citation check (the file must exist at that commit) applies unchanged.
"""
from __future__ import annotations

import fnmatch
import re

import httpx

from app.config import settings

#: Files whose first lines mark them generated (D1: "DO NOT EDIT" headers).
_GENERATED_HEADER = re.compile(r"do not edit|code generated|@generated|auto-?generated",
                               re.IGNORECASE)
LOCKFILES = ("uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
             "Cargo.lock", "Gemfile.lock", "go.sum", "composer.lock")
_FORGE_TIMEOUT = 5.0
ENGINE_RULE = "build.engine_evidence"
DONE_RULE = "merge.done_predicates"
_GITHUB_REPO = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


def repo_of(repo_url: str | None) -> str | None:
    """`owner/name` of a GitHub repo url (`github.com/o/r`, with or without scheme), else None."""
    m = _GITHUB_REPO.match((repo_url or "").strip())
    return m.group(1) if m else None


def unread(path: str, reason: str) -> dict:
    return {"path": path, "reason": reason}


def _cite(path: str, commit: str, lines: str = "1-1") -> list[dict]:
    return [{"kind": "file", "ref": {"path": path, "commit": commit, "lines": lines}}]


def _source(path: str, repo: str | None, commit: str) -> dict:
    out = {"path": path}
    if repo:
        out["url"] = f"https://github.com/{repo}/blob/{commit}/{path}"
    return out


def _tighten(rule_id: str, params: dict, why: str, path: str, commit: str, lines: str) -> dict:
    return {"kind": "tighten", "rule_id": rule_id, "params": params, "why": why,
            "citations": _cite(path, commit, lines)}


def _finding(bucket: str, stage: str, quote: str, path: str, repo: str | None, commit: str,
             **extra) -> dict:
    return {"bucket": bucket, "stage": stage, "quote": quote,
            "source": _source(path, repo, commit), **extra}


def owner_slug(owner: str) -> str:
    """A custom-rule slug for a CODEOWNERS owner: `@org/team-a` -> `codeowners-org-team-a`."""
    body = re.sub(r"[^a-z0-9]+", "-", owner.lower()).strip("-") or "owner"
    return ("codeowners-" + body)[:64].rstrip("-")


# ---- CODEOWNERS --------------------------------------------------------------------------

def parse_codeowners(text: str, path: str, *, commit: str, repo: str | None = None
                     ) -> tuple[list[dict], list[dict]]:
    """`(findings, unread_entries)`. A path owned by exactly one owner becomes one
    `exclusive_areas` proposal; ownership of review is guidance plus a `reviewer_property` gap.
    An entry pointing outside this repo is listed unread."""
    areas: dict[str, list[tuple[str, int]]] = {}
    skipped: list[dict] = []
    owned = 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        pattern, owners = parts[0], parts[1:]
        if "://" in pattern or pattern.startswith("..") or re.match(r"^[\w.-]+/[\w.-]+@", pattern):
            skipped.append(unread(f"{path}:{n}", "outside this project's repo"))
            continue
        if not owners:
            continue
        owned += 1
        concrete = pattern.strip("/")
        if len(owners) == 1 and concrete and not any(c in concrete for c in "*?["):
            areas.setdefault(owners[0], []).append((concrete, n))
    findings: list[dict] = []
    for owner, held in areas.items():
        lines = f"{held[0][1]}-{held[-1][1]}"
        findings.append(_finding(
            "proposed", "Plan", "; ".join(f"{p} owned only by {owner}" for p, _ in held[:5]),
            path, repo, commit,
            proposal={"kind": "new_rule", "primitive": "exclusive_areas",
                      "slug": owner_slug(owner),
                      "title": f"Areas {owner} alone owns",
                      "why": f"CODEOWNERS names {owner} as the only owner of these paths, so "
                             "one drain at a time works in each",
                      "params": {"paths": [p for p, _ in held]},
                      "citations": _cite(path, commit, lines)}))
    if owned:
        findings.append(_finding(
            "gap", "Review", f"{owned} CODEOWNERS entries name who reviews which path", path,
            repo, commit, label="review ownership",
            text="CODEOWNERS says who should review a path; no primitive binds a reviewer to "
                 "a path (`reviewer_property` takes a property, not an owner).",
            citations=_cite(path, commit)))
    return _one_engine(findings), skipped


# ---- GitHub Actions ----------------------------------------------------------------------

#: A reusable workflow or action in ANOTHER repo: `owner/repo/path...@ref`. `./...` is local
#: and `docker://` is an image. A bare published action (`actions/checkout@v4`) names no path.
_REMOTE_PATH = re.compile(r"^[\w.-]+/[\w.-]+/[^@]+@\S+$")
_TEST_RUN = re.compile(r"pytest|npm test|pnpm test|yarn test|go test|cargo test")
ENGINES = ("postgres", "mysql")


def _remote(uses: object) -> bool:
    return isinstance(uses, str) and not uses.startswith(("./", "docker://")) and \
        bool(_REMOTE_PATH.match(uses.strip()))


def _one_engine(findings: list[dict]) -> list[dict]:
    """`build.engine_evidence` has ONE engine, so the import proposes one `turn_on`: the first.
    The same engine again is dropped; another engine is a gap a person settles."""
    first: str | None = None
    out = []
    for f in findings:
        p = f.get("proposal") or {}
        if p.get("rule_id") != ENGINE_RULE:
            out.append(f)
            continue
        engine = p["params"].get("engine")
        if first is None:
            first = engine
            out.append(f)
        elif engine != first:
            out.append({k: v for k, v in f.items() if k != "proposal"} | {
                "bucket": "gap", "label": "engine evidence engine",
                "text": f"CI also runs the tests on {engine}; `{ENGINE_RULE}` takes one engine "
                        f"and this import proposes {first}.",
                "citations": p["citations"]})
    return out


def engine_path(patterns: list[str]) -> str | None:
    """The one directory every trigger pattern names, as the substring `build.engine_evidence`
    matches a touchpoint against: `db/**` -> `db/`. None when the patterns name more than one
    place, or a place that is not a directory prefix (`**/*.sql`), so a person sets it."""
    out: set[str] = set()
    for pat in patterns:
        lit = re.split(r"[*?\[]", pat, maxsplit=1)[0].lstrip("/")
        rest = pat.lstrip("/")[len(lit):]
        if not lit.endswith("/") or rest not in ("", "**", "**/*", "*"):
            return None
        out.add(lit)
    return out.pop() if len(out) == 1 else None


def parse_workflow(text: str, path: str, *, commit: str, repo: str | None = None
                   ) -> tuple[list[dict], list[dict]]:
    """A job that runs the tests against a database service is evidence the tests run on that
    engine: one `turn_on` of `build.engine_evidence` with that engine. The rule's `path` and
    `engine` declare no comparator, so a parser never proposes a CHANGED `path` (it would be
    `not_proposable`). Turning the rule on may set it: when the trigger paths name one directory
    (`db/**`), the `turn_on` carries the substring the gate matches a touchpoint against
    (`db/`), never a glob. Trigger paths that name more than that are reported as guidance.
    A reusable workflow or action that lives in another repo is listed unread, never dropped."""
    try:
        import yaml
        doc = yaml.safe_load(text)
    except ImportError:
        return [], [unread(path, "no YAML parser installed")]
    except Exception as e:  # noqa: BLE001 — malformed YAML is reported, not an empty workflow
        return [], [unread(path, f"could not parse: {str(e).splitlines()[0][:120]}")]
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        return [], [unread(path, "no `jobs` mapping")]
    on = doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on` key as True
    trig_paths: list[str] = []
    if isinstance(on, dict):
        for ev in ("pull_request", "push"):
            cfg = on.get(ev)
            if isinstance(cfg, dict) and isinstance(cfg.get("paths"), list):
                trig_paths += [p for p in cfg["paths"] if isinstance(p, str)
                               and p not in trig_paths]
    skipped: list[dict] = []
    seen: dict[str, str] = {}  # engine -> the first job that showed it
    for name, job in doc["jobs"].items():
        if not isinstance(job, dict):
            continue
        if _remote(job.get("uses")):
            skipped.append(unread(f"{path}: jobs.{name} uses {job['uses']}",
                                  "outside this project's repo"))
            continue
        steps = [s for s in job.get("steps") or [] if isinstance(s, dict)]
        for i, st in enumerate(steps):
            if _remote(st.get("uses")):
                skipped.append(unread(f"{path}: jobs.{name}.steps[{i}] uses {st['uses']}",
                                      "outside this project's repo"))
        services = job.get("services") if isinstance(job.get("services"), dict) else {}
        runs = " ".join(str(s.get("run", "")) for s in steps)
        if not _TEST_RUN.search(runs):
            continue
        for engine in ENGINES:
            if engine not in seen and any(
                    engine in k.lower() or engine in str(v.get("image", "")).lower()
                    for k, v in services.items() if isinstance(v, dict)):
                seen[engine] = name
    findings = []
    prefix = engine_path(trig_paths)
    for engine, name in seen.items():
        params = {"engine": engine} | ({"path": prefix} if prefix else {})
        why = f"CI job `{name}` already runs the tests on {engine}" + (
            f" for {', '.join(trig_paths)}" if prefix else "")
        findings.append(_finding(
            "proposed", "Build", f"job `{name}` runs tests against {engine}"
            + (f" for {', '.join(trig_paths)}" if prefix else ""), path, repo,
            commit, proposal={"kind": "turn_on", "rule_id": ENGINE_RULE, "params": params,
                              "why": why, "citations": _cite(path, commit)}))
        if trig_paths and not prefix:
            findings.append(_finding(
                "gap", "Build", f"job `{name}` runs on {engine} for " + ", ".join(trig_paths),
                path, repo, commit, label="engine evidence paths",
                text=f"CI runs the {engine} tests for {', '.join(trig_paths)}. "
                     f"`{ENGINE_RULE}` binds one path substring, set by a person.",
                citations=_cite(path, commit)))
    return findings, skipped


# ---- Branch protection -------------------------------------------------------------------

def parse_branch_protection(data: dict, branch: str, path: str, *, commit: str,
                            repo: str | None = None, cite_path: str | None = None
                            ) -> list[dict]:
    """Branch protection is enforced by GitHub: EXTERNAL findings. Required checks also
    propose `suite_green` as a done predicate, cited to `cite_path` (a file at the commit; the
    API answer is not a file, so with none the proposal is left out)."""
    findings = []
    reviews = (data.get("required_pull_request_reviews") or {})
    n = reviews.get("required_approving_review_count")
    if n:
        findings.append(_finding(
            "external", "Review", f"`{branch}` requires {n} approving review(s)", path, repo,
            commit, label="required reviews",
            text=f"GitHub requires {n} approving review(s) before merge on `{branch}`."))
    checks = (data.get("required_status_checks") or {})
    names = checks.get("contexts") or [c.get("context") for c in checks.get("checks") or []
                                       if isinstance(c, dict)]
    names = [c for c in names if c]
    if names:
        findings.append(_finding(
            "external", "Merge", f"`{branch}` requires checks: {', '.join(names)}", path, repo,
            commit, label="required checks",
            text="GitHub blocks merge until these checks pass: " + ", ".join(names)))
        if cite_path:
            findings.append(_finding(
                "proposed", "Merge", f"`{branch}` requires status checks", cite_path, repo,
                commit, proposal=_tighten(
                    "merge.done_predicates", {"predicates": ["suite_green"]},
                    "branch protection already requires CI checks to pass", cite_path, commit,
                    "1-1")))
    if data.get("required_linear_history", {}).get("enabled"):
        findings.append(_finding(
            "external", "Merge", f"`{branch}` requires linear history", path, repo, commit,
            label="merge method",
            text="Linear history is required, so merge commits are blocked: squash or rebase."))
    return findings


def fetch_branch_protection(repo: str, branch: str, token: str | None = None
                            ) -> tuple[dict | None, dict | None]:
    """`(protection, unread_entry)`. A token that cannot read it is unread, naming the
    permission; it is never read as "no branch protection"."""
    path = f"branch protection: {repo}@{branch}"
    token = token or settings.github_token
    if not token:
        return None, unread(path, "no GitHub token configured (needs repository permission "
                                  "`Administration: read`)")
    try:
        r = httpx.get(f"https://api.github.com/repos/{repo}/branches/{branch}/protection",
                      headers={"Accept": "application/vnd.github+json",
                               "Authorization": f"Bearer {token}"}, timeout=_FORGE_TIMEOUT)
    except httpx.HTTPError as e:
        return None, unread(path, f"could not reach GitHub: {type(e).__name__}")
    if r.status_code == 200:
        return r.json(), None
    if r.status_code in (401, 403):
        return None, unread(path, "could not read branch protection: token lacks "
                                  "admin:read (`Administration: read`)")
    if r.status_code == 404:
        # GitHub answers 404 both for "not protected" and "token cannot see it".
        return None, unread(path, "GitHub returned 404: the branch is unprotected or the "
                                  "token lacks `Administration: read`")
    return None, unread(path, f"GitHub answered {r.status_code}")


# ---- Generated-file markers --------------------------------------------------------------

def parse_gitattributes(text: str, path: str, *, commit: str, repo: str | None = None
                        ) -> list[dict]:
    globs = []
    for raw in text.splitlines():
        parts = raw.split("#", 1)[0].split()
        if len(parts) >= 2 and any(a in ("linguist-generated", "linguist-generated=true")
                                   for a in parts[1:]):
            globs.append(parts[0].lstrip("/"))
    if not globs:
        return []
    return [_finding(
        "proposed", "Build", "linguist-generated: " + ", ".join(globs[:5]), path, repo, commit,
        proposal=_tighten("build.generated_files", {"paths": globs},
                          "`.gitattributes` marks these paths linguist-generated", path, commit,
                          "1-1"))]


def generated_paths(files: dict[str, str], *, head_lines: int = 5) -> list[str]:
    """Files whose header says DO NOT EDIT, plus lockfiles present."""
    out = [p for p in files if p.rsplit("/", 1)[-1] in LOCKFILES]
    for p, body in files.items():
        if p in out or p.rsplit("/", 1)[-1] in LOCKFILES:
            continue
        if _GENERATED_HEADER.search("\n".join(body.splitlines()[:head_lines])):
            out.append(p)
    return sorted(out)


def parse_generated_markers(files: dict[str, str], *, commit: str, repo: str | None = None
                            ) -> list[dict]:
    paths = generated_paths(files)
    if not paths:
        return []
    return [_finding(
        "proposed", "Build", "generated: " + ", ".join(paths[:5]), paths[0], repo, commit,
        proposal=_tighten("build.generated_files", {"paths": paths},
                          "lockfiles and DO-NOT-EDIT headers mark these paths generated",
                          paths[0], commit, "1-5"))]


# ---- Current values ----------------------------------------------------------------------

LIST_RULES = ("build.generated_files", "merge.done_predicates")


def current_params(db, project_id: str) -> dict[str, dict]:
    """The project's adopted params for the rules a parser proposes against, read once per run.
    `build.engine_evidence` also carries `_on`, whether it is already on."""
    from app.services import rule_toggles as T
    out: dict[str, dict] = {}
    for rid in (*LIST_RULES, ENGINE_RULE):
        if rid in T.LIBRARY:
            st = T.resolve(db, rid, project_id)
            # GRPH-1971: a default naming this repo's own paths is not the project's value.
            own = not T.repo_path_default(rid, st)
            out[rid] = dict(st["params"]["value"] or {}) if own else {}
            if rid == ENGINE_RULE:
                out[rid]["_on"] = bool(st["on"]["value"])
    return out


def _engine_against_current(f: dict, cur: dict) -> dict | None:
    """A `turn_on` of a rule that is already on is no difference: dropped when the engine
    matches, a gap naming both engines when it does not (the param has no comparator, so only
    a person changes it)."""
    if not cur.get("_on") or (not cur.get("path") and not cur.get("engine")):
        return f
    want, have = f["proposal"]["params"].get("engine"), cur.get("engine") or "postgres"
    want_path, have_path = f["proposal"]["params"].get("path"), cur.get("path")
    if want == have:
        if not want_path or not have_path or want_path == have_path:
            return None
        return {k: v for k, v in f.items() if k != "proposal"} | {
            "bucket": "gap", "label": "engine evidence paths",
            "text": f"CI runs the {want} tests for {want_path}; `{ENGINE_RULE}` is on for "
                    f"{have_path}. The path has no comparator, so a person changes it.",
            "citations": f["proposal"]["citations"]}
    return {k: v for k, v in f.items() if k != "proposal"} | {
        "bucket": "gap", "label": "engine evidence engine",
        "text": f"CI runs the tests on {want}; `{ENGINE_RULE}` is on with engine {have}. "
                "The engine has no comparator, so a person changes it.",
        "citations": f["proposal"]["citations"]}


def _against_current(findings: list[dict], current: dict[str, dict]) -> list[dict]:
    """A proposal replaces a list param whole, so it carries the adopted entries plus the new
    ones. One that adds nothing to what is adopted is not a difference and is dropped."""
    out = []
    for f in findings:
        p = f.get("proposal")
        cur = current.get(p.get("rule_id")) if p else None
        if cur is not None and p["rule_id"] == ENGINE_RULE:
            f = _engine_against_current(f, cur)
            if f is None:
                continue
        elif cur is not None and p["rule_id"] in LIST_RULES:
            key = "paths" if "paths" in p["params"] else "predicates"
            have = list(cur.get(key) or [])
            new = [x for x in p["params"][key] if x not in have]
            if not new:
                continue
            f = {**f, "proposal": {**p, "params": {key: have + new}}}
        out.append(f)
    return out


# ---- The whole read ----------------------------------------------------------------------

def read_sources(files: dict[str, str], *, commit: str, repo: str | None = None,
                 branch: str = "main", protection: dict | None = None,
                 protection_unread: dict | None = None,
                 current: dict[str, dict] | None = None) -> dict:
    """Run every parser over `files` (path -> text). Returns the body `create_run` takes:
    `sources_read`, `sources_unread`, `findings`. Prose sources are the skill's, not read here."""
    read: list[dict] = []
    unreads: list[dict] = []
    findings: list[dict] = []
    for p, body in sorted(files.items()):
        base = p.rsplit("/", 1)[-1]
        if base == "CODEOWNERS":
            f, u = parse_codeowners(body, p, commit=commit, repo=repo)
        elif fnmatch.fnmatch(p, ".github/workflows/*.y*ml"):
            f, u = parse_workflow(body, p, commit=commit, repo=repo)
        elif base == ".gitattributes":
            f, u = parse_gitattributes(body, p, commit=commit, repo=repo), []
        else:
            continue
        findings += f
        unreads += u
        read.append({"path": p})
    findings = _one_engine(findings)
    gen = parse_generated_markers(files, commit=commit, repo=repo)
    if gen:
        findings += gen
        read.append({"path": gen[0]["source"]["path"]})
    if protection is not None:
        findings += parse_branch_protection(protection, branch, f"branch protection: {branch}",
                                            commit=commit, repo=repo,
                                            cite_path=next((p for p in sorted(files) if
                                                            p.startswith(".github/workflows/")),
                                                           None))
        read.append({"path": f"branch protection: {branch}"})
    elif protection_unread is not None:
        unreads.append(protection_unread)
    if current is not None:
        findings = _against_current(findings, current)
    return {"commit": commit, "sources_read": read, "sources_unread": unreads,
            "findings": findings, "clean": not unreads}
