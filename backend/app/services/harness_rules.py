"""Recommendations as drafts, and the replay that makes each one checkable (PRD-38 D7).

Named rules read the cells that PR 2 rolls up and produce cards. A card is a draft: it
carries the cells it fired on, the rule and its thresholds, the sibling cells it did NOT fire
on, and a **replay** — what the proposed change would have done to the resolutions that
actually happened. Accepting one is a human act through a surface that already exists (a
commit to the matrix, a PUT to a profile or a policy). Nothing here changes anything.

PRD-41 S4 adds R5 (a cell vs its per-capability prior) and R6 (a better measured row
dropped as not installed, and the unenforceable-cap shape), re-keys R3 to the family
rule (D18) and R4 per capability.

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
#: D18: a reorder drafts only when the family-level beat holds across this many families.
REWEIGHT_MIN_FAMILIES = 2
#: R4: a `local_only` project whose local rows bounce this often, or succeed this often.
POLICY_MIN_N, POLICY_BOUNCE_RATE, POLICY_KEEP_RATE = 6, 0.7, 0.7
#: R5 (D9): a cell at the floor that disagrees with the row's per-capability prior.
REPRIOR_MIN_N, REPRIOR_MARGIN = 5, 0.3
#: R6 (D17): this many resolutions dropped a better measured row as not installed.
INSTALL_MIN_DROPS, INSTALL_MARGIN = 6, 0.3

RULES = ("R1", "R2", "R3", "R4", "R5", "R6")


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

def _rows_in_window(db: Session, project_ids: list[str],
                    window_days: int | None) -> list[AttemptTelemetry]:
    from datetime import timedelta

    from sqlalchemy import select

    if not project_ids:
        return []
    cutoff = harness_svc._now() - timedelta(
        days=harness_svc.WINDOW_DAYS if window_days is None else window_days)
    rows = db.scalars(select(AttemptTelemetry).where(
        AttemptTelemetry.project_id.in_(project_ids),
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


def cards(db: Session, project_id: str | None = None, *, org_id: str | None = None,
          window_days: int | None = None) -> list[Card]:
    """Every card the rules produce for this project (or org), most-severe first.

    Rules never pool across capability or size band (D4): each fires on ONE cell, its drafted
    text names that cell, and the sibling cells it did not fire on travel with it so nobody
    reads a claim about `A4/L` as a claim about the harness. At org scope every card names
    the projects its evidence came from (criterion 11).
    """
    if org_id:
        report = harness_svc.org_report(db, org_id, window_days=window_days, versions="all")
        project_ids = list(report.get("projects") or [])
        scope_id = org_id
    else:
        if not project_id:
            return []
        report = harness_svc.report(db, project_id, window_days=window_days, versions="all",
                                    overlay=True)
        project_ids = [project_id]
        scope_id = project_id
    rows = _rows_in_window(db, project_ids, window_days)
    statuses = _status_seen(rows)
    out: list[Card] = []
    out += _promote_and_demote(report, rows, statuses)
    out += _reweight(db, scope_id, report, rows)
    out += _policy(db, project_ids, report, rows)
    out += _reprior(report, rows, statuses)
    out += _availability(db, report, rows)
    out += _probe_suggestions(report)
    if org_id:
        for card in out:
            names = _projects_of(card, report)
            if names:
                card.draft = {**card.draft, "projects": names}
                card.detail = (card.detail.rstrip(".")
                               + f". Evidence from {', '.join(names)}.")
    order = {"R2": 0, "R6": 1, "R4": 2, "R5": 3, "R1": 4, "R3": 5, "probe": 6}
    return sorted(out, key=lambda c: (order.get(c.rule, 9), c.key))


def _projects_of(card: Card, report: dict) -> list[str]:
    seen: list[str] = []
    for block in list(card.cells) + list(card.siblings):
        cell_key = block.get("cell") if isinstance(block, dict) else None
        if not isinstance(cell_key, dict):
            continue
        for cell in report.get("cells") or []:
            if cell.get("key") != cell_key:
                continue
            for row in cell.get("by_project") or []:
                pid = row.get("project_id")
                if pid and pid not in seen:
                    seen.append(pid)
    return seen


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


def _family_rates(report: dict) -> dict[tuple[str, str], dict]:
    """Pooled finished/signed_off per (vendor:model, family). D18's family-level view."""
    out: dict[tuple[str, str], dict] = {}
    for cell in report["cells"]:
        if cell["rate"] is None:
            continue
        fam = harness_svc.family_of(cell["key"]["capability"])
        key = (_cell_name(cell), fam)
        seen = out.setdefault(key, {"finished": 0, "signed_off": 0, "cells": []})
        seen["finished"] += cell["finished"]
        seen["signed_off"] += cell["signed_off"]
        seen["cells"].append(cell)
    for seen in out.values():
        seen["rate"] = (round(seen["signed_off"] / seen["finished"], 3)
                        if seen["finished"] else None)
    return out


def _reweight(db: Session, scope_id: str, report: dict,
              rows: list[AttemptTelemetry]) -> list[Card]:
    """R3 under D18: reorder only when the top default is beaten at family level
    across ≥ 2 families. A single-capability beat stays on the grid and drafts nothing.
    """
    from sqlalchemy import select

    out: list[Card] = []
    profiles = db.scalars(select(FleetProfile).where(
        (FleetProfile.project_id == scope_id) | (FleetProfile.project_id.is_(None)))).all()
    family_rates = _family_rates(report)
    for profile in profiles:
        defaults = list(profile.defaults or [])
        if not defaults:
            continue
        top = defaults[0]
        top_families = {fam: seen for (name, fam), seen in family_rates.items()
                        if name.split(":")[0] == top or name.startswith(f"{top}:")}
        rivals: dict[str, list[str]] = {}
        for (name, fam), seen in family_rates.items():
            harness = name.split(":")[0]
            if harness == top:
                continue
            if seen["rate"] is None or seen["finished"] < REWEIGHT_MIN_N:
                continue
            top_seen = top_families.get(fam)
            if (top_seen is None or top_seen["rate"] is None
                    or top_seen["finished"] < REWEIGHT_MIN_N):
                continue
            if seen["rate"] - top_seen["rate"] >= REWEIGHT_MARGIN:
                rivals.setdefault(name, []).append(fam)
        for rival_name, families in rivals.items():
            if len(set(families)) < REWEIGHT_MIN_FAMILIES:
                continue
            reordered = [rival_name.split(":")[0]] + [d for d in defaults
                                                      if d != rival_name.split(":")[0]]
            cells = []
            for fam in sorted(set(families)):
                cells.extend(_cell_out(c) for c in family_rates[(rival_name, fam)]["cells"][:1])
                top_key = next((k for k in family_rates if k[1] == fam
                                and k[0].split(":")[0] == top), None)
                if top_key:
                    cells.extend(_cell_out(c) for c in family_rates[top_key]["cells"][:1])
            out.append(Card(
                rule="R3",
                title=f"reorder {profile.user_id}'s defaults: {rival_name} above {top}",
                detail=(f"{rival_name} beats {top} at the family level across "
                        f"{len(set(families))} families ({', '.join(sorted(set(families)))}), "
                        f"and {top} is this profile's first default."),
                cells=cells,
                siblings=[],
                draft={"target": f"{profile.user_id}:{profile.project_id or 'default'}",
                       "kind": "profile_defaults", "defaults": reordered,
                       "user_id": profile.user_id, "project_id": profile.project_id,
                       "families": sorted(set(families)),
                       "where": "PUT /api/fleet/profile"},
                replay=replay(rows, {"kind": "defaults", "defaults": reordered}),
                thresholds={"min_n": REWEIGHT_MIN_N, "margin": REWEIGHT_MARGIN,
                            "min_families": REWEIGHT_MIN_FAMILIES}))
    return out


def _policy(db: Session, project_ids: list[str], report: dict,
            rows: list[AttemptTelemetry]) -> list[Card]:
    """R4 per capability: what `local_only` (or a policy drop) is costing, or earning."""
    out: list[Card] = []
    local_names = {n for n, local in _locality(rows).items() if local}
    by_cap: dict[str, list[dict]] = {}
    for cell in report["cells"]:
        if cell["rate"] is None or _cell_name(cell) not in local_names:
            continue
        by_cap.setdefault(cell["key"]["capability"], []).append(cell)
    for project_id in project_ids:
        project = db.get(Project, project_id)
        policy = (project.fleet_policy or {}) if project is not None else {}
        local_only = bool(policy.get("local_only"))
        for cap, cells in by_cap.items():
            finished = sum(c["finished"] for c in cells)
            signed = sum(c["signed_off"] for c in cells)
            if finished < POLICY_MIN_N:
                continue
            rate = signed / finished
            cell = max(cells, key=lambda c: c["finished"])
            name = _cell_name(cell)
            if local_only and (1.0 - rate) >= POLICY_BOUNCE_RATE:
                out.append(Card(
                    rule="R4",
                    title=f"consider lifting local_only for {cap}",
                    detail=(f"{name} is the local row this policy keeps, and in {cap} it "
                            f"bounced {finished - signed} of {finished}. The replay says "
                            f"what lifting the policy would have changed; it does not say "
                            f"those attempts would have gone better."),
                    cells=[_cell_out(c) for c in cells], siblings=_siblings(report, cell),
                    draft={"target": project_id, "kind": "project_policy", "local_only": False,
                           "capability": cap, "project_id": project_id,
                           "where": "PUT /api/fleet/policy"},
                    replay=replay(rows, {"kind": "policy", "local_only": False}),
                    thresholds={"min_n": POLICY_MIN_N, "bounce_rate": POLICY_BOUNCE_RATE}))
            elif not local_only and rate >= POLICY_KEEP_RATE:
                out.append(Card(
                    rule="R4",
                    title=f"consider local_only for {cap}",
                    detail=(f"{name} runs locally and signed off {signed} of {finished} "
                            f"in {cap}. Turning `local_only` on would keep work on it; "
                            f"the replay says which resolutions that changes."),
                    cells=[_cell_out(c) for c in cells], siblings=_siblings(report, cell),
                    draft={"target": project_id, "kind": "project_policy", "local_only": True,
                           "capability": cap, "project_id": project_id,
                           "where": "PUT /api/fleet/policy"},
                    replay=replay(rows, {"kind": "policy", "local_only": True}),
                    thresholds={"min_n": POLICY_MIN_N, "keep_rate": POLICY_KEEP_RATE}))
        out.extend(_policy_drops(project_id, report, rows))
    return out


def _policy_drops(project_id: str, report: dict, rows: list[AttemptTelemetry]) -> list[Card]:
    """A row repeatedly dropped by policy for a capability feeds R4 for that capability."""
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        res = row.resolution if isinstance(row.resolution, dict) else None
        if not res:
            continue
        caps = list(res.get("capabilities") or []) or ["other"]
        for dropped in res.get("dropped_rows") or []:
            if dropped.get("stage") != "policy":
                continue
            name = _name(dropped)
            if not name:
                continue
            for cap in caps:
                counts[(name, cap)] = counts.get((name, cap), 0) + 1
    out: list[Card] = []
    for (name, cap), n in counts.items():
        if n < POLICY_MIN_N:
            continue
        cell = next((c for c in report["cells"]
                     if _cell_name(c) == name and c["key"]["capability"] == cap), None)
        out.append(Card(
            rule="R4",
            title=f"policy dropped {name} for {cap} {n} times",
            detail=(f"{name} was dropped by project policy on {n} resolutions that needed "
                    f"{cap}. The control is PUT /api/fleet/policy."),
            cells=[_cell_out(cell)] if cell else [],
            siblings=_siblings(report, cell) if cell else [],
            draft={"target": project_id, "kind": "project_policy", "capability": cap,
                   "dropped": name, "drops": n, "project_id": project_id,
                   "where": "PUT /api/fleet/policy"},
            replay=replay(rows, {"kind": "policy", "local_only": False}),
            thresholds={"min_n": POLICY_MIN_N, "drops": n}))
    return out


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


def _rule_cells(report: dict) -> list[dict]:
    """Leaves where the grid nested them; otherwise the cell itself.

    R5 names a capability. A family rollup is not a matrix evidence line.
    """
    out: list[dict] = []
    for cell in report.get("cells") or []:
        leaves = cell.get("leaves") or []
        if leaves:
            out.extend(leaves)
        else:
            out.append(cell)
    return out


def _prior_rate(status: str | None) -> float | None:
    """Committed-row prior as a number. Unverified is not a prior — R5 drafts from the cell."""
    if status == "verified":
        return 1.0
    if status == "failed":
        return 0.0
    return None


def _side(cell: dict, which: str) -> dict:
    samples = cell.get("samples") if isinstance(cell.get("samples"), dict) else {}
    side = samples.get(which) if isinstance(samples, dict) else None
    if isinstance(side, dict) and "finished" in side:
        return side
    if which == "natural":
        return {"finished": cell.get("finished") or 0, "signed_off": cell.get("signed_off") or 0,
                "rate": cell.get("rate")}
    return {"finished": 0, "signed_off": 0, "rate": None}


def _reprior(report: dict, rows: list[AttemptTelemetry],
             statuses: dict[str, str]) -> list[Card]:
    """R5 (D9): a cell at n≥5 that disagrees with the row's per-capability prior by ≥0.3.

    A leaf with no prior drafts an evidence line from the cell. Probes count with their
    label; a probe cell under the floor fires nothing (criterion 27). The hash covers
    the cells, not just the proposal (the PRD-38 defect, retested).
    """
    out: list[Card] = []
    for cell in _rule_cells(report):
        name = _cell_name(cell)
        cap = cell["key"]["capability"]
        status = statuses.get(name)
        prior = _prior_rate(status)
        natural, probe = _side(cell, "natural"), _side(cell, "probe")
        candidates = []
        if (natural.get("finished") or 0) >= REPRIOR_MIN_N and natural.get("rate") is not None:
            candidates.append(("natural", natural))
        if (probe.get("finished") or 0) >= REPRIOR_MIN_N and probe.get("rate") is not None:
            candidates.append(("probe", probe))
        if not candidates:
            continue
        for kind, side in candidates:
            rate = side["rate"]
            if prior is None:
                disagree = True
            else:
                disagree = abs(rate - prior) >= REPRIOR_MARGIN
            if not disagree:
                continue
            probe_labelled = kind == "probe"
            evidence = (f"{cap}: {side['signed_off']}/{side['finished']} signed off "
                        f"(rate {rate})"
                        + (f" vs prior {status} ({prior})" if prior is not None
                           else " — no per-capability prior; draft from the cell")
                        + (" [probe]" if probe_labelled else ""))
            out.append(Card(
                rule="R5",
                title=f"reprior {name} for {cap}",
                detail=(f"{name}'s {cap} cell is {side['signed_off']}/{side['finished']} "
                        + (f"against a {status} prior of {prior}." if prior is not None
                           else "and the row has no per-capability prior.")
                        + (" Probe samples contributed; they are labelled, not mixed into "
                           "the natural rate." if probe_labelled else "")
                        + " R5 drafts an evidence entry naming the capability and never "
                          "flips the row's status."),
                cells=[_cell_out(cell)], siblings=_siblings(report, cell),
                draft={"target": name, "kind": "matrix_evidence", "capability": cap,
                       "binary_version": cell["key"].get("binary_version") or "",
                       "evidence_line": evidence, "probe": probe_labelled,
                       "where": "fleet/src/gbfleet/matrix.toml"},
                replay=replay(rows, {"kind": "status", "target": name,
                                     "status": status or "unverified"}),
                thresholds={"min_n": REPRIOR_MIN_N, "margin": REPRIOR_MARGIN}))
    return out


def _measured_grade(report: dict, name: str, cap: str,
                    db: Session | None = None) -> tuple[float, str, int] | None:
    """A grade from a measured layer: project, org, platform or probe — never prior alone.

    A dropped-as-not-installed row has no local cell. The grade has to come from
    org/platform/probe tables, not only from what the grid already rendered.
    """
    for cell in _rule_cells(report):
        if _cell_name(cell) != name:
            continue
        cell_cap = cell["key"]["capability"]
        if cell_cap != cap and harness_svc.family_of(cell_cap) != cap and cell_cap != harness_svc.family_of(cap):
            continue
        probe = _side(cell, "probe")
        if (probe.get("finished") or 0) >= harness_svc.FLOOR and probe.get("rate") is not None:
            return float(probe["rate"]), "probe", int(probe["finished"])
        plat = cell.get("platform") if isinstance(cell.get("platform"), dict) else None
        if plat and plat.get("rate") is not None:
            n = plat.get("n") or plat.get("orgs") or harness_svc.FLOOR
            n_int = n if isinstance(n, int) else harness_svc.PLATFORM_MIN_N
            return float(plat["rate"]), "platform", int(n_int)
        natural = _side(cell, "natural")
        if (natural.get("finished") or 0) >= harness_svc.FLOOR and natural.get("rate") is not None:
            layer = "org" if (cell.get("by_project") and len(cell["by_project"]) > 1) else "project"
            return float(natural["rate"]), layer, int(natural["finished"])
    if db is not None:
        from sqlalchemy import select as _select

        vendor, _, model = name.partition(":")
        from app.models import CapabilityPrior, PlatformRollup
        plat = db.scalars(_select(PlatformRollup).where(
            PlatformRollup.vendor == vendor, PlatformRollup.model == model,
            PlatformRollup.capability == cap)).all()
        finished = sum(r.finished for r in plat)
        signed = sum(r.signed_off for r in plat)
        if finished >= harness_svc.PLATFORM_MIN_N and signed >= 0:
            agg = {"orgs": max((r.orgs_contributing for r in plat), default=0),
                   "finished": finished, "signed_off": signed,
                   "top_share": max((r.top_org_share or 0.0 for r in plat), default=0.0)}
            served = harness_svc._platform_cell(agg)
            if served and served.get("rate") is not None:
                return float(served["rate"]), "platform", finished
        prior = db.scalars(_select(CapabilityPrior).where(
            CapabilityPrior.vendor == vendor, CapabilityPrior.model == model,
            CapabilityPrior.capability == cap)).first()
        if prior is not None and prior.rate is not None:
            # A fetched snapshot is a measured platform layer, labelled as one.
            return float(prior.rate), "platform", harness_svc.PLATFORM_MIN_N
    return None


def _availability(db: Session, report: dict, rows: list[AttemptTelemetry]) -> list[Card]:
    """R6 (D17): ≥6 resolutions dropped a better MEASURED row as not installed.

    The grade must come from org, platform or probe (or a project cell at the floor) —
    a row known only from the committed prior draws no card. The unenforceable-cap
    shape (every eligible row unreporting) is the same rule in different clothes.
    """
    drops: dict[tuple[str, str], list[AttemptTelemetry]] = {}
    unenforceable: dict[str, dict] = {}
    for row in rows:
        res = row.resolution if isinstance(row.resolution, dict) else None
        if not res:
            continue
        caps = list(res.get("capabilities") or []) or list(row.capabilities or []) or ["other"]
        if res.get("unenforceable_cap"):
            cap_name = (res["unenforceable_cap"] or {}).get("cap") or "cap"
            unenforceable.setdefault(cap_name, res["unenforceable_cap"])
        winner = res.get("winner") or ((res.get("shortlist") or [None])[0])
        winner_name = _name(winner) if isinstance(winner, dict) else ""
        for dropped in res.get("dropped_rows") or []:
            why = (dropped.get("why") or "").lower()
            stage = dropped.get("stage") or ""
            if stage != "installed" and "not installed" not in why:
                continue
            name = _name(dropped)
            if not name:
                continue
            for cap in caps:
                drops.setdefault((name, cap, winner_name), []).append(row)
    out: list[Card] = []
    seen: set[tuple[str, str]] = set()
    for (name, cap, winner_name), hit_rows in drops.items():
        if len(hit_rows) < INSTALL_MIN_DROPS:
            continue
        if (name, cap) in seen:
            continue
        dropped_grade = _measured_grade(report, name, cap, db)
        if dropped_grade is None:
            continue
        d_rate, d_layer, d_n = dropped_grade
        winner_grade = _measured_grade(report, winner_name, cap, db) if winner_name else None
        w_rate, w_layer, w_n = winner_grade if winner_grade else (0.0, "unknown", 0)
        if d_rate - w_rate < INSTALL_MARGIN:
            continue
        seen.add((name, cap))
        remedy = f"install or serve {name} for {cap}"
        cell = next((c for c in _rule_cells(report)
                     if _cell_name(c) == name and c["key"]["capability"] == cap), None)
        out.append(Card(
            rule="R6",
            title=f"install {name} for {cap}",
            detail=(f"{len(hit_rows)} resolutions dropped {name} as not installed for {cap}. "
                    f"Its measured grade is {d_rate:.2f} from {d_layer} (n={d_n}) against "
                    f"{winner_name or 'the winner'}'s {w_rate:.2f} from {w_layer} (n={w_n}). "
                    f"Remedy: {remedy}."),
            cells=[_cell_out(cell)] if cell else [],
            siblings=_siblings(report, cell) if cell else [],
            draft={"target": name, "kind": "install", "capability": cap,
                   "drops": len(hit_rows), "dropped_grade": d_rate, "dropped_layer": d_layer,
                   "winner": winner_name, "winner_grade": w_rate, "winner_layer": w_layer,
                   "remedy": remedy, "where": "gbfleet doctor"},
            replay=replay(hit_rows, {"kind": "status", "target": name, "status": "verified"}),
            thresholds={"min_drops": INSTALL_MIN_DROPS, "margin": INSTALL_MARGIN}))
    for cap_name, payload in unenforceable.items():
        listed = payload.get("rows") or []
        out.append(Card(
            rule="R6",
            title=f"{cap_name} is unenforceable on the rows this project allows",
            detail=(f"Every eligible row is unreporting tokens under {cap_name}. "
                    f"The rows and their reporting shares: "
                    + ", ".join(f"{r.get('key') or r.get('harness')} "
                                f"({r.get('reporting_share')})" for r in listed)
                    + ". No row is scored on cost_class under that cap."),
            cells=[], siblings=[],
            draft={"target": cap_name, "kind": "cap_unenforceable", "cap": cap_name,
                   "rows": listed, "where": "PUT /api/fleet/policy"},
            replay={"considered": 0, "changed": 0, "skipped_no_resolution": 0,
                    "truncated": False, "moves": [],
                    "summary": "a refused resolution has no winner to replay"},
            thresholds={"min_drops": 1}))
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
