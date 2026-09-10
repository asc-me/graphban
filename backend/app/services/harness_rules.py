"""Recommendations as drafts, and the replay that makes each one checkable (PRD-38 D7).

Four named rules read the cells that PR 2 rolls up and produce cards. A card is a draft: it
carries the cells it fired on, the rule and its thresholds, the sibling cells it did NOT fire
on, and a **replay** — what the proposed change would have done to the resolutions that
actually happened. Accepting one is a human act through a surface that already exists (a
commit to the matrix, a PUT to a profile or a policy). Nothing here changes anything.

**The replay re-ranks recorded resolutions.** PRD-37's resolver lives in the supervisor's
package with the committed matrix beside it; the server has neither, and simulating today's
matrix over last month's attempts would answer with a matrix, a profile and an installed set
that have all moved since. So every launch records its own explanation — the scored shortlist,
every drop with the score it would have had, the profile that applied — and the replay
re-ranks that. It says what the change does to work that really happened, which is the only
thing a replay can honestly say.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import AttemptTelemetry, FleetProfile, Project
from app.services import harness as harness_svc

# ---- thresholds, in one place so the page can show them ---------------------------------------

#: R1: an unverified row this good, this often, is worth a `verified` commit.
PROMOTE_MIN_FINISHED, PROMOTE_MIN_RATE = 10, 0.8
#: R2: a verified row this bad is worth a `failed` entry.
DEMOTE_MIN_FINISHED, DEMOTE_MAX_RATE = 6, 0.25
#: R3: a profile's top default beaten by this much, on this many attempts, in the same cell.
REWEIGHT_MIN_N, REWEIGHT_MARGIN = 8, 0.3
#: R4: a `local_only` project whose local rows bounce this often, or succeed this often.
POLICY_MIN_N, POLICY_BOUNCE_RATE, POLICY_KEEP_RATE = 6, 0.7, 0.7

RULES = ("R1", "R2", "R3", "R4")


@dataclass
class Card:
    """One drafted recommendation. `evidence_hash` is what makes accept and dismiss expire."""

    rule: str
    title: str
    detail: str
    cells: list[dict] = field(default_factory=list)
    siblings: list[dict] = field(default_factory=list)
    draft: dict = field(default_factory=dict)
    replay: dict = field(default_factory=dict)
    thresholds: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Rule + the thing it is about + the version it is about. Stable across evidence."""
        target = self.draft.get("target") or ""
        version = self.draft.get("binary_version") or ""
        return f"{self.rule}:{target}:{version}"

    @property
    def evidence_hash(self) -> str:
        """A digest of the NUMBERS behind the card. When they move, the card comes back —
        which is how an accepted recommendation whose evidence later reverses says so."""
        payload = json.dumps([self.cells, self.siblings, self.draft], sort_keys=True,
                             default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def as_dict(self) -> dict:
        return {"rule": self.rule, "key": self.key, "evidence_hash": self.evidence_hash,
                "title": self.title, "detail": self.detail, "cells": self.cells,
                "siblings": self.siblings, "draft": self.draft, "replay": self.replay,
                "thresholds": self.thresholds}


# ---- the replay -------------------------------------------------------------------------------

#: A replay reads at most this many recorded resolutions, and says so when it stops. Sampling
#: silently would make the one number on the card that is meant to be checkable a guess.
REPLAY_MAX_ROWS = 5000


def _rank_key(entry: dict, defaults: list[str]) -> tuple:
    """PRD-37 D5's tie-break, over a RECORDED candidate rather than a live row.

    Score, then verified over unverified, then the user's defaults order, then matrix order —
    the same four, in the same order, because a replay that ranked differently from the
    resolver would be describing a system nobody runs.
    """
    verified = 1 if entry.get("status") == "verified" else 0
    harness = entry.get("harness") or ""
    pref = -defaults.index(harness) if harness in defaults else 0
    return (entry.get("score") or 0.0, verified, pref, -(entry.get("order") or 0))


def _winner(shortlist: list[dict], defaults: list[str]) -> dict | None:
    if not shortlist:
        return None
    return sorted(shortlist, key=lambda e: _rank_key(e, defaults), reverse=True)[0]


def _name(entry: dict | None) -> str:
    if not entry:
        return ""
    return f"{entry.get('harness') or ''}:{entry.get('model') or ''}"


def replay(rows: list[AttemptTelemetry], change: dict) -> dict:
    """What the change would have done to the resolutions that actually happened.

    `change` is one of:
      `{"kind": "status", "target": "harness:model", "status": "verified"|"failed"}`
      `{"kind": "defaults", "defaults": [...]}`
      `{"kind": "policy", "local_only": bool}`

    Reports how many resolutions differ and to what. It does NOT claim they would have gone
    better — a replay cannot know that, and a card that implied it would be selling a guess as
    a measurement.
    """
    considered = changed = skipped = 0
    moves: dict[tuple[str, str], int] = {}
    truncated = False
    for row in rows:
        if considered >= REPLAY_MAX_ROWS:
            truncated = True
            break
        res = row.resolution if isinstance(row.resolution, dict) else None
        shortlist = list(res.get("shortlist") or []) if res else []
        if not shortlist:
            # An attempt whose resolution was never recorded (an explicit adapter, a launch
            # post that never landed, anything from before this shipped) cannot be replayed.
            # Counted and named, never quietly treated as unchanged.
            skipped += 1
            continue
        considered += 1
        profile = res.get("profile") if isinstance(res.get("profile"), dict) else {}
        defaults = list(profile.get("defaults") or [])
        before = _winner(shortlist, defaults)

        after_list = [dict(e) for e in shortlist]
        after_defaults = defaults
        if change["kind"] == "status":
            target = change["target"]
            if change["status"] == "failed":
                after_list = [e for e in after_list if _name(e) != target]
            else:
                for e in after_list:
                    if _name(e) == target:
                        e["status"] = change["status"]
        elif change["kind"] == "defaults":
            after_defaults = list(change["defaults"])
            after_list = [e for e in after_list
                          if not after_defaults or (e.get("harness") in after_defaults)]
        elif change["kind"] == "policy":
            dropped = [e for e in (res.get("dropped_rows") or [])
                       if e.get("stage") == "policy"]
            if change.get("local_only") is False:
                after_list = after_list + [dict(e) for e in dropped]
            else:
                after_list = [e for e in after_list if e.get("local")]

        after = _winner(after_list, after_defaults)
        if _name(before) != _name(after):
            changed += 1
            moves[(_name(before), _name(after) or "nothing")] = \
                moves.get((_name(before), _name(after) or "nothing"), 0) + 1
    return {
        "considered": considered,
        "changed": changed,
        "skipped_no_resolution": skipped,
        "truncated": truncated,
        "moves": [{"from": a, "to": b, "count": n} for (a, b), n in sorted(moves.items())],
        # Said in words on the card, because "0 of 31" and "31 of 31" are both honest answers
        # that a reader will otherwise take as an error.
        "summary": (f"would have changed {changed} of {considered} recorded resolutions"
                    + (f"; {skipped} had no recorded resolution to replay" if skipped else "")),
    }


# ---- the rules --------------------------------------------------------------------------------

def _rows_in_window(db: Session, project_id: str, window_days: int | None) -> list[AttemptTelemetry]:
    from datetime import timedelta

    from sqlalchemy import select

    cutoff = harness_svc._now() - timedelta(
        days=harness_svc.WINDOW_DAYS if window_days is None else window_days)
    rows = db.scalars(select(AttemptTelemetry).where(
        AttemptTelemetry.project_id == project_id,
        AttemptTelemetry.derived_at.is_not(None))).all()
    return [r for r in rows if harness_svc._aware(r.derived_at) >= cutoff]


def _status_seen(rows: list[AttemptTelemetry]) -> dict[str, str]:
    """What the matrix said about each harness:model, as recorded at launch.

    The server holds no matrix — it is a committed file in the supervisor's package — so the
    only honest source for a row's status is what the resolutions actually recorded. A row
    nobody ever resolved has no cells either, so no rule could fire on it in any case.
    """
    out: dict[str, str] = {}
    for row in rows:
        res = row.resolution if isinstance(row.resolution, dict) else None
        if not res:
            continue
        for entry in list(res.get("shortlist") or []) + list(res.get("dropped_rows") or []):
            name = _name(entry)
            if name and entry.get("status"):
                out[name] = entry["status"]
    return out


def _cell_name(cell: dict) -> str:
    k = cell["key"]
    return f"{k['vendor']}:{k['model']}"


def _label(cell: dict) -> str:
    k = cell["key"]
    return f"{k['capability']}/{k['size_band']}"


def cards(db: Session, project_id: str, *, window_days: int | None = None) -> list[Card]:
    """Every card the four rules produce for this project, most-severe first.

    Rules never pool across task class or size band (D4): each fires on ONE cell, its drafted
    text names that cell, and the sibling cells it did not fire on travel with it so nobody
    reads a claim about `backend/L` as a claim about the harness.
    """
    report = harness_svc.report(db, project_id, window_days=window_days, versions="all")
    rows = _rows_in_window(db, project_id, window_days)
    statuses = _status_seen(rows)
    out: list[Card] = []
    out += _promote_and_demote(report, rows, statuses)
    out += _reweight(db, project_id, report, rows)
    out += _policy(db, project_id, report, rows)
    out += _probe_suggestions(report)
    order = {"R2": 0, "R4": 1, "R1": 2, "R3": 3, "probe": 4}
    return sorted(out, key=lambda c: (order.get(c.rule, 9), c.key))


def _probe_suggestions(report: dict) -> list[Card]:
    """D8: a newly declared vendor/model/version, never a schedule (criterion 9)."""
    out: list[Card] = []
    for sug in report.get("probe_suggestions") or []:
        target = f"{sug['vendor']}:{sug['model']}"
        out.append(Card(
            rule="probe",
            title=f"Probe {target}",
            detail=sug.get("reason") or "a harness first resolved with no cell for it",
            draft={"target": target, "trigger": sug["trigger"],
                   "binary_version": sug.get("binary_version") or "",
                   "estimated_tokens": sug.get("estimated_tokens"),
                   "scheduled": False},
            thresholds={"scheduled": False, "floor": harness_svc.FLOOR},
        ))
    return out


def _siblings(report: dict, cell: dict) -> list[dict]:
    """The other bands and classes of the same vendor:model, as context.

    A promote that fires on `backend/L` is a claim about `backend/L`. The matrix cannot
    express a band, so accepting it generalises to the lane — and the card has to show what it
    is generalising over rather than let a reader assume it does not matter.
    """
    name = _cell_name(cell)
    return [{"cell": c["key"], "finished": c["finished"], "signed_off": c["signed_off"],
             "rate": c["rate"], "below_floor": c["below_floor"]}
            for c in report["cells"]
            if _cell_name(c) == name and c["key"] != cell["key"]]


def _cell_out(cell: dict) -> dict:
    return {"cell": cell["key"], "finished": cell["finished"], "signed_off": cell["signed_off"],
            "rate": cell["rate"], "sampling": cell["sampling"], "skew": cell["skew"]}


def _promote_and_demote(report: dict, rows: list[AttemptTelemetry],
                        statuses: dict[str, str]) -> list[Card]:
    out: list[Card] = []
    for cell in report["cells"]:
        name = _cell_name(cell)
        status = statuses.get(name)
        rate = cell["rate"]
        if rate is None or status is None:
            continue
        if (status == "unverified" and cell["finished"] >= PROMOTE_MIN_FINISHED
                and rate >= PROMOTE_MIN_RATE):
            out.append(Card(
                rule="R1",
                title=f"promote {name} for {cell['key']['capability']}",
                detail=(f"{name} signed off {cell['signed_off']} of {cell['finished']} in "
                        f"{_label(cell)} and its matrix row is still unverified. The matrix "
                        f"keys on harness and model, not on size band, so a verified row here "
                        f"generalises to {cell['key']['capability']} — the sibling cells below "
                        f"are what it would be generalising over."),
                cells=[_cell_out(cell)], siblings=_siblings(report, cell),
                draft={"target": name, "kind": "matrix_status", "status": "verified",
                       "binary_version": cell["key"]["binary_version"],
                       "evidence_line": (f"verified: {cell['signed_off']}/{cell['finished']} "
                                         f"signed off in {_label(cell)}"),
                       "where": "fleet/src/gbfleet/matrix.toml"},
                replay=replay(rows, {"kind": "status", "target": name, "status": "verified"}),
                thresholds={"min_finished": PROMOTE_MIN_FINISHED, "min_rate": PROMOTE_MIN_RATE}))
        if (status == "verified" and cell["finished"] >= DEMOTE_MIN_FINISHED
                and rate <= DEMOTE_MAX_RATE):
            out.append(Card(
                rule="R2",
                title=f"demote {name} for {cell['key']['capability']}",
                detail=(f"{name} signed off only {cell['signed_off']} of {cell['finished']} in "
                        f"{_label(cell)} and its matrix row is verified."),
                cells=[_cell_out(cell)], siblings=_siblings(report, cell),
                draft={"target": name, "kind": "matrix_status", "status": "failed",
                       "binary_version": cell["key"]["binary_version"],
                       "evidence_line": (f"failed: {cell['signed_off']}/{cell['finished']} "
                                         f"signed off in {_label(cell)}"),
                       "where": "fleet/src/gbfleet/matrix.toml"},
                replay=replay(rows, {"kind": "status", "target": name, "status": "failed"}),
                thresholds={"min_finished": DEMOTE_MIN_FINISHED, "max_rate": DEMOTE_MAX_RATE}))
    return out


def _reweight(db: Session, project_id: str, report: dict,
              rows: list[AttemptTelemetry]) -> list[Card]:
    """R3: a profile whose top default is beaten in the same cell.

    Per profile, not per project: a profile is its owner's, and the card at any other scope is
    informational (D7's open question 4, answered the same way).
    """
    from sqlalchemy import select

    out: list[Card] = []
    profiles = db.scalars(select(FleetProfile).where(
        (FleetProfile.project_id == project_id) | (FleetProfile.project_id.is_(None)))).all()
    for profile in profiles:
        defaults = list(profile.defaults or [])
        if not defaults:
            continue
        top = defaults[0]
        for cell in report["cells"]:
            if cell["key"]["vendor"] != top and _cell_name(cell).split(":")[0] != top:
                continue
            if cell["rate"] is None or cell["finished"] < REWEIGHT_MIN_N:
                continue
            rivals = [c for c in report["cells"]
                      if c["key"]["capability"] == cell["key"]["capability"]
                      and c["key"]["size_band"] == cell["key"]["size_band"]
                      and _cell_name(c) != _cell_name(cell)
                      and c["rate"] is not None and c["finished"] >= REWEIGHT_MIN_N
                      and c["rate"] - cell["rate"] >= REWEIGHT_MARGIN]
            for rival in rivals:
                rival_name = _cell_name(rival)
                reordered = [rival_name.split(":")[0]] + [d for d in defaults
                                                          if d != rival_name.split(":")[0]]
                out.append(Card(
                    rule="R3",
                    title=f"reorder {profile.user_id}'s defaults: {rival_name} above {top}",
                    detail=(f"In {_label(cell)}, {rival_name} signed off "
                            f"{rival['signed_off']}/{rival['finished']} against "
                            f"{_cell_name(cell)}'s {cell['signed_off']}/{cell['finished']}, "
                            f"and {top} is this profile's first default."),
                    cells=[_cell_out(rival), _cell_out(cell)],
                    siblings=_siblings(report, rival),
                    draft={"target": f"{profile.user_id}:{profile.project_id or 'default'}",
                           "kind": "profile_defaults", "defaults": reordered,
                           "user_id": profile.user_id, "project_id": profile.project_id,
                           "where": "PUT /api/fleet/profile"},
                    replay=replay(rows, {"kind": "defaults", "defaults": reordered}),
                    thresholds={"min_n": REWEIGHT_MIN_N, "margin": REWEIGHT_MARGIN}))
    return out


def _policy(db: Session, project_id: str, report: dict,
            rows: list[AttemptTelemetry]) -> list[Card]:
    """R4: what the project's `local_only` is costing, or earning."""
    project = db.get(Project, project_id)
    policy = (project.fleet_policy or {}) if project is not None else {}
    local_only = bool(policy.get("local_only"))
    local_names = {n for n, local in _locality(rows).items() if local}
    for cell in report["cells"]:
        name = _cell_name(cell)
        if name not in local_names or cell["rate"] is None:
            continue
        if cell["finished"] < POLICY_MIN_N:
            continue
        bounced_rate = 1.0 - cell["rate"]
        if local_only and bounced_rate >= POLICY_BOUNCE_RATE:
            return [Card(
                rule="R4",
                title=f"consider lifting local_only for {cell['key']['capability']}",
                detail=(f"{name} is the local row this policy keeps, and in {_label(cell)} it "
                        f"bounced {cell['finished'] - cell['signed_off']} of "
                        f"{cell['finished']}. The replay says what lifting the policy would "
                        f"have changed; it does not say those attempts would have gone better."),
                cells=[_cell_out(cell)], siblings=_siblings(report, cell),
                draft={"target": project_id, "kind": "project_policy", "local_only": False,
                       "project_id": project_id, "where": "PUT /api/fleet/policy"},
                replay=replay(rows, {"kind": "policy", "local_only": False}),
                thresholds={"min_n": POLICY_MIN_N, "bounce_rate": POLICY_BOUNCE_RATE})]
        if not local_only and cell["rate"] >= POLICY_KEEP_RATE:
            return [Card(
                rule="R4",
                title=f"consider local_only for {cell['key']['capability']}",
                detail=(f"{name} runs locally and signed off {cell['signed_off']} of "
                        f"{cell['finished']} in {_label(cell)}. Turning `local_only` on would "
                        f"keep work on it; the replay says which resolutions that changes."),
                cells=[_cell_out(cell)], siblings=_siblings(report, cell),
                draft={"target": project_id, "kind": "project_policy", "local_only": True,
                       "project_id": project_id, "where": "PUT /api/fleet/policy"},
                replay=replay(rows, {"kind": "policy", "local_only": True}),
                thresholds={"min_n": POLICY_MIN_N, "keep_rate": POLICY_KEEP_RATE})]
    return []


def _locality(rows: list[AttemptTelemetry]) -> dict[str, bool]:
    """Which harness:model the matrix called local, as recorded at launch."""
    out: dict[str, bool] = {}
    for row in rows:
        res = row.resolution if isinstance(row.resolution, dict) else None
        if not res:
            continue
        for entry in list(res.get("shortlist") or []) + list(res.get("dropped_rows") or []):
            name = _name(entry)
            if name and entry.get("local") is not None:
                out[name] = bool(entry.get("local"))
    return out


def lesson_text(card: Card) -> str:
    """A PRD-16 shaped lesson from an accepted card.

    Names the cells and the counts, never the rule id: an agent reading this needs to know
    what was measured, and "R1 fired" is a fact about this module rather than about the work.
    """
    parts = []
    for c in card.cells:
        cell = c["cell"]
        parts.append(f"{cell['vendor']}:{cell['model']} signed off "
                     f"{c['signed_off']}/{c['finished']} in {cell['capability']}/"
                     f"{cell['size_band']}")
    return (f"{card.title}. " + "; ".join(parts) + ". "
            + (card.replay.get("summary") or "") + ".").replace("..", ".")
