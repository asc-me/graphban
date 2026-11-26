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
_GENERATED_HEADER = re.compile(r"do not edit|code generated|@generated|auto-?generated", re.I)
LOCKFILES = ("uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
             "Cargo.lock", "Gemfile.lock", "go.sum", "composer.lock")
_FORGE_TIMEOUT = 5.0


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


# ---- CODEOWNERS --------------------------------------------------------------------------

def parse_codeowners(text: str, path: str, *, commit: str, repo: str | None = None
                     ) -> tuple[list[dict], list[dict]]:
    """`(findings, unread_entries)`. A path owned by exactly one owner becomes one
    `exclusive_areas` proposal; ownership of review is guidance plus a `reviewer_property` gap.
    An entry pointing outside this repo is listed unread."""
    areas: list[tuple[str, int]] = []
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
            areas.append((concrete, n))
    findings: list[dict] = []
    if areas:
        lines = f"{areas[0][1]}-{areas[-1][1]}"
        findings.append(_finding(
            "proposed", "Plan", "; ".join(f"{p} owned by one owner" for p, _ in areas[:5]),
            path, repo, commit,
            proposal={"kind": "tighten", "rule_id": "plan.exclusive_areas",
                      "params": {"paths": [p for p, _ in areas]},
                      "why": "CODEOWNERS gives each of these paths a single owner",
                      "citations": _cite(path, commit, lines)}))
    if owned:
        findings.append(_finding(
            "gap", "Review", f"{owned} CODEOWNERS entries name who reviews which path", path,
            repo, commit, label="review ownership",
            text="CODEOWNERS says who should review a path; no primitive binds a reviewer to "
                 "a path (`reviewer_property` takes a property, not an owner).",
            citations=_cite(path, commit)))
    return findings, skipped


# ---- GitHub Actions ----------------------------------------------------------------------

def parse_workflow(text: str, path: str, *, commit: str, repo: str | None = None
                   ) -> tuple[list[dict], list[dict]]:
    """A job that runs the tests against a Postgres service is evidence the tests run on that
    engine: a tighter `build.engine_evidence` for the paths the workflow triggers on."""
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
                trig_paths += [p for p in cfg["paths"] if isinstance(p, str)]
    findings = []
    for name, job in doc["jobs"].items():
        if not isinstance(job, dict):
            continue
        services = job.get("services") if isinstance(job.get("services"), dict) else {}
        runs = " ".join(str(s.get("run", "")) for s in job.get("steps") or []
                        if isinstance(s, dict))
        for engine in ("postgres", "mysql"):
            if any(engine in k.lower() or engine in str(v.get("image", "")).lower()
                   for k, v in services.items() if isinstance(v, dict)) and \
                    re.search(r"pytest|npm test|pnpm test|go test|cargo test", runs):
                for glob in (trig_paths or ["**"])[:3]:
                    findings.append(_finding(
                        "proposed", "Build", f"job `{name}` runs tests against {engine}", path,
                        repo, commit,
                        proposal=_tighten("build.engine_evidence",
                                          {"path": glob, "engine": engine},
                                          f"CI job `{name}` runs the tests on {engine}", path,
                                          commit, "1-1")))
    return findings, []


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

LIST_RULES = ("plan.exclusive_areas", "build.generated_files", "merge.done_predicates")


def current_params(db, project_id: str) -> dict[str, dict]:
    """The project's adopted params for the rules a parser tightens, read once per run."""
    from app.services import rule_toggles as T
    out: dict[str, dict] = {}
    for rid in (*LIST_RULES, "build.engine_evidence"):
        if rid in T.LIBRARY:
            out[rid] = dict(T.resolve(db, rid, project_id)["params"]["value"] or {})
    return out


def _against_current(findings: list[dict], current: dict[str, dict]) -> list[dict]:
    """A proposal replaces a list param whole, so it carries the adopted entries plus the new
    ones. One that adds nothing to what is adopted is not a difference and is dropped."""
    out = []
    for f in findings:
        p = f.get("proposal")
        cur = current.get(p["rule_id"]) if p else None
        if cur is not None and p["rule_id"] in LIST_RULES:
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
