"""What is ACTUALLY installed, not only what this pipeline generated (GRPH-354 / PRD-16).

`usage_report` and `stale_artifacts` read `ArtifactRecommendation` rows, and that table only
ever holds artifacts **Graphban produced**. Every skill, hook, agent and rule a human wrote
by hand was invisible to it — so the retirement half was measuring its own footprint. On a
fresh install it reported a population of zero while the operator's `.claude/` directory held
dozens of real artifacts, and those are the ones consuming context on every single turn,
which is the cost PRD-16 exists to control.

**The scan runs where the files are, and posts its findings up.** Not on the server: under
`hosted_mode` the server has no access to anyone's `.claude/` directory, and inside the
docker-compose container it has no access to the host's either. A server-side walk would
report a population of zero *without erroring* — the same "absence reads as a clean result"
failure this module exists to fix, reintroduced by the fix.

**Read-only, and it stays read-only.** Nothing here opens a file for writing, creates a
directory, or unlinks anything, under any input. A discovered artifact enters the population
count and becomes eligible for the staleness *read*; it never becomes eligible for automatic
action. An inventoried file that has since disappeared is flagged `orphaned` and left alone.

**Fork detection is the half that matters most.** `install_plan` re-renders machine-owned
artifacts in FULL rather than patching, so an artifact a human has edited by hand must never
be re-rendered — that silently discards their edit, which is precisely the trust failure the
propose-only boundary was built to prevent. Nothing detected that state before this.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ArtifactInventoryItem, ArtifactRecommendation

logger = logging.getLogger(__name__)

# What each kind of artifact looks like on disk. Ordered: the first pattern that matches a
# path wins, so a more specific location is listed before a more general one.
#
# Tiers are the SAME vocabulary `artifacts.py` classifies into, deliberately — a discovered
# rule has to be unmeasurable for exactly the reason a generated one is, and two tier
# vocabularies would let the two halves disagree about that.
TIER_RULES: tuple[tuple[str, str], ...] = (
    # Claude Code
    (".claude/skills/*/SKILL.md", "skill"),
    (".claude/agents/*.md", "agent"),
    (".claude/hooks/*", "hook"),
    (".claude/rules/*.md", "rule"),
    # Cursor keeps rules under a different root and extension.
    (".cursor/rules/*.mdc", "rule"),
    (".cursor/rules/*.md", "rule"),
)

# Files that are artifacts by NAME wherever they sit — the shared documents a `rule` tier
# recommendation targets. Matched on basename, since a repo keeps them at its top level.
NAMED_RULE_FILES = ("AGENTS.md", "CLAUDE.md", ".cursorrules")

# A cap, for the same reason ingest has one: a root pointed at a home directory by accident
# should stop rather than walk a million files. Reported when it bites — a silent truncation
# would read as "that is everything installed", which is the claim this module makes.
MAX_FILES = 5000


@dataclass
class Discovered:
    """One artifact found on disk. `path` is as the scanner saw it, absolute."""

    path: str
    tier: str
    content_hash: str
    size: int

    def as_dict(self) -> dict:
        return {"path": self.path, "tier": self.tier,
                "content_hash": self.content_hash, "size": self.size}


def content_hash(text: str) -> str:
    """sha256 of the contents, with trailing whitespace normalised.

    Normalised because an editor that adds a final newline is not a human forking an
    artifact, and a fork flag that fires on that is one people learn to ignore — at which
    point it stops protecting the edits it exists to protect.
    """
    return hashlib.sha256(text.rstrip().encode("utf-8", "replace")).hexdigest()


def _tier_of(path: Path, root: Path) -> str | None:
    """Which tier this file is, or None if it is not an artifact at all.

    Matched against the path RELATIVE to the root's parent, so a root of `~/.claude` and a
    root of `~` both produce `.claude/skills/x/SKILL.md` and classify identically. Without
    that, the same file inventoried under two roots would land in two different tiers.
    """
    if path.name in NAMED_RULE_FILES:
        return "rule"
    anchor = root.parent if root.name.startswith(".") else root
    try:
        rel = path.relative_to(anchor)
    except ValueError:
        rel = path
    for pattern, tier in TIER_RULES:
        if rel.match(pattern):
            return tier
    return None


def scan(roots: list[str]) -> tuple[list[Discovered], dict]:
    """Walk `roots` and inventory every artifact found. Never writes anything.

    Returns the findings and a stats dict. Unreadable files are skipped and counted rather
    than raising: a root is somebody's home directory, and one permission-denied file must
    not cost the operator the whole inventory.
    """
    found: list[Discovered] = []
    stats = {"roots": 0, "files": 0, "skipped": 0, "truncated": False}
    seen: set[str] = set()

    for raw in roots:
        root = Path(os.path.expanduser(raw)).resolve()
        if not root.is_dir():
            logger.info("inventory: root %s is not a directory; skipping", raw)
            continue
        stats["roots"] += 1
        # `rglob` yields nothing for an unreadable or missing tree rather than raising —
        # the same property `ClaudeCodeAdapter.discover` relies on.
        for path in sorted(root.rglob("*")):
            if len(found) >= MAX_FILES:
                stats["truncated"] = True
                logger.warning("inventory: stopped at %d files under %s", MAX_FILES, root)
                break
            if not path.is_file():
                continue
            tier = _tier_of(path, root)
            if tier is None:
                continue
            key = str(path.resolve())
            if key in seen:  # two overlapping roots must not double-count one file
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                logger.info("inventory: could not read %s; skipping", path)
                stats["skipped"] += 1
                continue
            seen.add(key)
            stats["files"] += 1
            found.append(Discovered(path=key, tier=tier,
                                    content_hash=content_hash(text), size=len(text)))
    return found, stats


# ---- recording a scan ---------------------------------------------------------------------

def _generated_index(db: Session, project_id: str | None) -> list[ArtifactRecommendation]:
    """Generated artifacts that could plausibly be on disk — approved, with a rendered draft
    and a path. A queued recommendation has not been installed by anyone yet, so a file at
    its path is somebody else's, not a fork of ours."""
    rows = db.scalars(select(ArtifactRecommendation).where(
        ArtifactRecommendation.status == "approved")).all()
    return [r for r in rows
            if r.draft and r.draft_path
            and (project_id is None or r.project_id in (project_id, None))]


def _match_generated(path: str, generated: list[ArtifactRecommendation]):
    """The generated artifact this file IS, or None.

    Matched on the path TAIL. A recommendation stores a repo-relative `draft_path`
    (`.claude/skills/foo/SKILL.md`) while a scan reports an absolute one, and there is no
    reliable way to recover the operator's repo root from either. The tail is distinctive
    enough in practice — `.claude/skills/<slug>/SKILL.md` — and the failure mode of being
    wrong is conservative in the direction that matters: an unmatched file is treated as
    human-authored, which is the state that REFUSES automated re-rendering.
    """
    for rec in generated:
        tail = rec.draft_path.lstrip("./")
        if tail and path.replace(os.sep, "/").endswith(tail):
            return rec
    return None


def record_scan(db: Session, *, project_id: str | None, root: str,
                items: list[dict], now: datetime | None = None) -> dict:
    """Fold one root's scan into the inventory. Writes rows; never touches the filesystem.

    Orphaning is scoped to `root` for a reason worth stating: a scan of `~/.claude` says
    nothing whatsoever about what is under `~/work/.cursor`, and marking those missing
    because this pass did not look there would be an absence read as a finding.
    """
    now = now or datetime.now(timezone.utc)
    generated = _generated_index(db, project_id)
    existing = {r.path: r for r in db.scalars(select(ArtifactInventoryItem).where(
        ArtifactInventoryItem.root == root)).all()
        if project_id is None or r.project_id in (project_id, None)}

    stats = {"seen": 0, "added": 0, "updated": 0, "forked": 0, "orphaned": 0}
    for item in items:
        path = str(item.get("path") or "")
        if not path:
            continue
        stats["seen"] += 1
        rec = _match_generated(path, generated)
        # A machine-owned file whose contents no longer match what we rendered has been
        # edited by a human. That is not a problem to correct — it is a boundary to respect.
        forked = rec is not None and content_hash(rec.draft) != str(item.get("content_hash"))
        state = "forked" if forked else "present"
        if forked:
            stats["forked"] += 1

        row = existing.pop(path, None)
        if row is None:
            row = ArtifactInventoryItem(project_id=project_id, root=root, path=path,
                                        first_seen=now)
            db.add(row)
            stats["added"] += 1
        else:
            stats["updated"] += 1
        row.tier = str(item.get("tier") or "")
        row.content_hash = str(item.get("content_hash") or "")
        row.size = int(item.get("size") or 0)
        row.recommendation_id = rec.id if rec is not None else None
        row.state = state
        row.last_seen = now

    # Whatever is left was inventoried under this root before and is not there now.
    for row in existing.values():
        if row.state != "orphaned":
            stats["orphaned"] += 1
        row.state = "orphaned"
        row.last_seen = now

    db.commit()
    return stats


def inventory(db: Session, project_id: str | None = None,
              include_orphaned: bool = True) -> list[ArtifactInventoryItem]:
    rows = db.scalars(select(ArtifactInventoryItem)).all()
    out = [r for r in rows if project_id is None or r.project_id in (project_id, None)]
    if not include_orphaned:
        out = [r for r in out if r.state != "orphaned"]
    return sorted(out, key=lambda r: r.path)


def fork_of(db: Session, rec: ArtifactRecommendation) -> ArtifactInventoryItem | None:
    """The inventory row saying a human has edited this generated artifact, if there is one.

    Consulted by `install_plan`, which re-renders in FULL. Without this it would overwrite
    the edit and report success.
    """
    return db.scalars(select(ArtifactInventoryItem).where(
        ArtifactInventoryItem.recommendation_id == rec.id,
        ArtifactInventoryItem.state == "forked")).first()
