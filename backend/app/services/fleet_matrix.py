"""Committed preference-matrix rows and this deployment's tier map (GRPH-866, GRPH-1003).

The supervisor still loads `fleet/src/gbfleet/matrix.toml` to resolve spawns. The web app
cannot read that file (it talks to this API, not the supervisor), so the same facts are
listed here for display. Status still moves by a commit to the toml; this list is the
catalog the page draws, not a second resolver.

Unique on (harness, model, tier). Duplicate toml rows are not a second cell.

**The tier map (GRPH-1003).** The catalog is facts and stays read-only. Beside it, a
deployment may pin *which model a harness runs for a tier* — the panel the design draws and
the control that never shipped, so the map was retuned by hand in package data inside the
published wheel. `TierOverride` rows layer ON TOP of `ROWS`; they never replace them, and
clearing them deletes rows rather than writing empty ones, so "cleared" and "never set" are
one state and neither can be served as a map that routes nothing.

An override PINS a committed row, it does not invent one: `set_overrides` refuses a model the
catalog does not already name for that harness and tier. Nothing here can add a harness, and
nothing here can score a cell — grading is read off measured attempts and a cell with nothing
above `GRADE_FLOOR` says `not measured` rather than borrowing the packaged default.
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TierOverride, utcnow

# Facts only. Taste is fleet_profiles; constraints are fleet_policy.
ROWS: tuple[dict, ...] = (
    {
        "harness": "gbagent",
        "model": "qwen3.6:35b-a3b-coding-mtp-det",
        "vendor": "gbagent",
        "lane": "any",
        "tier": "cheap",
        "status": "verified",
        "cost_class": "local",
        "local": True,
    },
    {
        "harness": "gbagent",
        "model": "qwen3-coder:30b",
        "vendor": "gbagent",
        "lane": "any",
        "tier": "cheap",
        "status": "failed",
        "cost_class": "local",
        "local": True,
    },
    {
        "harness": "claude",
        "model": "sonnet",
        "vendor": "anthropic",
        "lane": "any",
        "tier": "cheap",
        "status": "unverified",
        "cost_class": "cheap",
        "local": False,
    },
    {
        "harness": "qwen-code",
        "model": "",
        "vendor": "alibaba",
        "lane": "any",
        "tier": "cheap",
        "status": "verified",
        "cost_class": "cheap",
        "local": False,
    },
    {
        "harness": "cursor-agent",
        "model": "composer-2.5",
        "vendor": "cursor",
        "lane": "any",
        "tier": "cheap",
        "status": "unverified",
        "cost_class": "cheap",
        "local": False,
    },
    {
        "harness": "claude",
        "model": "opus",
        "vendor": "anthropic",
        "lane": "any",
        "tier": "frontier",
        "status": "unverified",
        "cost_class": "frontier",
        "local": False,
    },
    {
        "harness": "grok",
        "model": "grok-4.5",
        "vendor": "xai",
        "lane": "any",
        "tier": "frontier",
        "status": "unverified",
        "cost_class": "frontier",
        "local": False,
    },
    {
        "harness": "codex",
        "model": "",
        "vendor": "openai",
        "lane": "any",
        "tier": "frontier",
        "status": "unregistered",
        "cost_class": "frontier",
        "local": False,
    },
)


#: Finished attempts behind a measured cell before *Inherit from performance grading* will
#: name a model from it. The same floor the supervisor's own resolver uses
#: (`gbfleet.matrix.MIN_SAMPLE`), restated rather than imported: the fleet is a separate
#: distribution with its own install (PRD-22 D-e) and the backend must not depend on it.
GRADE_FLOOR = 5


class TierMapInvalid(ValueError):
    """A tier map that says something it may not — refused BEFORE anything is written, so a
    half-applied map is not a state this can reach."""


def _packaged() -> list[dict]:
    """One cell per harness × tier the catalog names, with the models it offers for it.

    Catalog order, which mirrors the committed matrix's own `order`. `models` is EMPTY for a
    cell whose rows name no model (qwen-code/cheap, codex/frontier today) — the harness runs
    its own default and there is nothing to pin. That is a fact about the catalog, not a gap
    in this function, so the cell is served and the panel says so.
    """
    out: dict[tuple[str, str], dict] = {}
    for row in ROWS:
        key = (str(row["harness"]), str(row["tier"]))
        cell = out.setdefault(key, {"harness": key[0], "tier": key[1], "models": []})
        model = str(row.get("model") or "")
        if model and model not in cell["models"]:
            cell["models"].append(model)
    return list(out.values())


def _graded(db: Session | None, project_id: str | None) -> dict[str, str]:
    """`{harness: best-measured model}` — what *Inherit from performance grading* fills from.

    Joined on (vendor, model), which is what a measured cell carries and what a catalog row
    declares, so nothing here guesses at a pairing. A harness with no cell above
    `GRADE_FLOOR` is ABSENT, and absent reaches the panel as `not measured` — never as a zero
    and never as the packaged model wearing a grading's clothes.
    """
    if db is None:
        return {}
    from app.services import delegation as delegation_svc

    harness_of = {(str(r["vendor"]), str(r.get("model") or "")): str(r["harness"]) for r in ROWS}
    best: dict[str, tuple[float, str]] = {}
    for cell in delegation_svc.measured(db, project_id) or []:
        quality = cell.get("quality")
        if not isinstance(quality, dict) or quality.get("value") is None:
            continue
        try:
            if int(quality.get("n") or 0) < GRADE_FLOOR:
                continue
            score = float(quality["value"])
        except (TypeError, ValueError):
            continue
        model = str(cell.get("model") or "")
        harness = harness_of.get((str(cell.get("vendor") or ""), model))
        if not harness or not model:
            continue
        if harness not in best or score > best[harness][0]:
            best[harness] = (score, model)
    return {harness: model for harness, (_score, model) in best.items()}


def overrides_for(db: Session | None, project_id: str | None) -> dict:
    """What rides on `fleet_status`: the stored overrides, and nothing else.

    Always present, always a list. Empty means the packaged matrix governs — and the
    supervisor tells that apart from "the server could not be reached", which never reaches
    this function at all (`gbfleet.mcp.read_status` catches it and says so).
    """
    if db is None or not project_id:
        return {"overrides": [], "overridden": False}
    rows = db.scalars(select(TierOverride).where(
        TierOverride.project_id == project_id)).all()
    out = [{"harness": r.harness, "tier": r.tier, "model": r.model,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None}
           for r in sorted(rows, key=lambda r: (r.harness, r.tier))]
    return {"overrides": out, "overridden": bool(out)}


def payload(db: Session | None = None, project_id: str | None = None) -> dict:
    """What `GET /api/fleet` carries as `matrix`, and what `/api/fleet/tier-map` returns.

    Always present; empty would mean unlooked. `rows` are the committed facts and `cells` are
    the harness × tier map drawn over them, each cell carrying the packaged model, the stored
    override, what the fleet will therefore run, and what grading would pick.

    Called with no session it answers from the catalog alone — every cell unoverridden, every
    cell unmeasured — which is what a caller with no project has, and is a true statement
    rather than a missing one.
    """
    saved = {(o["harness"], o["tier"]): o["model"]
             for o in overrides_for(db, project_id)["overrides"]}
    graded = _graded(db, project_id)
    cells: list[dict] = []
    for cell in _packaged():
        key = (cell["harness"], cell["tier"])
        override = saved.get(key)
        packaged = cell["models"][0] if cell["models"] else ""
        # Grading names a model per HARNESS. It reaches a cell only when that model is one the
        # catalog offers for the cell's tier: inheriting anything else would write an override
        # `set_overrides` must refuse, so the button would look like it worked and save nothing.
        best = graded.get(cell["harness"])
        cells.append({
            **cell,
            "packaged_model": packaged,
            "override": override,
            "effective_model": packaged if override is None else override,
            "overridden": override is not None,
            "graded_model": best if best in cell["models"] else None,
        })
    return {"rows": [dict(row) for row in ROWS], "cells": cells, "overridden": bool(saved)}


def set_overrides(db: Session, project_id: str, cells_in: list[dict] | None) -> dict:
    """Replace this project's tier map. Returns the map that results.

    PUT semantics: a cell absent from the body, or sent with an empty model, is CLEARED — the
    panel saves its whole draft, so what is not in it is not wanted. Every cell is validated
    before any row is written, because a map half-applied is a routing nobody can read back.

    Refuses a model the catalog does not name for that harness and tier. The override decides
    WHICH committed row a tier runs; it cannot make a row exist, and a pin to a model nothing
    verified would be an invention with a status attached.
    """
    offered = {(c["harness"], c["tier"]): list(c["models"]) for c in _packaged()}
    wanted: dict[tuple[str, str], str] = {}
    for entry in cells_in or []:
        entry = entry if isinstance(entry, dict) else {}
        harness = str(entry.get("harness") or "").strip()
        tier = str(entry.get("tier") or "").strip()
        raw = entry.get("model")
        model = str(raw).strip() if raw is not None else ""
        if not harness or not tier:
            raise TierMapInvalid("a cell names its harness and its tier")
        key = (harness, tier)
        if key in wanted:
            raise TierMapInvalid(f"{harness}/{tier} is listed twice")
        if not model:
            wanted[key] = ""
            continue
        if key not in offered:
            raise TierMapInvalid(
                f"{harness}/{tier} is not a cell the catalog names — the tier map overrides "
                "the committed matrix, it does not extend it")
        if model not in offered[key]:
            named = ", ".join(offered[key]) or "no model at all"
            raise TierMapInvalid(
                f"{harness}/{tier}: the catalog names {named} for {harness} in tier {tier}, "
                f"not {model!r}. An override pins a committed row; adding one is a commit to "
                "fleet/src/gbfleet/matrix.toml")
        wanted[key] = model
    pins = {k: v for k, v in wanted.items() if v}

    by_cell = {(r.harness, r.tier): r for r in db.scalars(select(TierOverride).where(
        TierOverride.project_id == project_id)).all()}
    for key, model in pins.items():
        row = by_cell.get(key)
        if row is None:
            db.add(TierOverride(id=f"to_{uuid4().hex[:12]}", project_id=project_id,
                                harness=key[0], tier=key[1], model=model, updated_at=utcnow()))
        elif row.model != model:
            row.model = model
            row.updated_at = utcnow()
    for key, row in by_cell.items():
        if key not in pins:
            db.delete(row)
    db.commit()
    return payload(db, project_id)


def clear_overrides(db: Session, project_id: str) -> dict:
    """Delete every override and return the map that remains — the PACKAGED one.

    Deleting rather than blanking is the whole point. The reply is what the panel redraws and
    what a test asserts against, so it has to be the committed matrix and never an empty cell
    list: an empty tier map routes nothing, and serving one back from a *clear* would make
    "you now have no fleet" the reassuring reading of a button labelled cleanup.
    """
    for row in db.scalars(select(TierOverride).where(
            TierOverride.project_id == project_id)).all():
        db.delete(row)
    db.commit()
    return payload(db, project_id)


def harnesses() -> list[str]:
    """Unique harness names that can take mix share, excluding unregistered."""
    seen: list[str] = []
    for row in ROWS:
        if row["status"] == "unregistered":
            continue
        name = str(row["harness"])
        if name not in seen:
            seen.append(name)
    return seen
