"""The preference matrix and the resolver (PRD-37 D2, D5–D8, D14–D19).

Three layers, three owners, and this module joins them at spawn time:

- **Facts** — `matrix.toml`, committed, one row per harness × model × lane × tier with a
  status and the items that proved it. Loaded by `load()`.
- **Policy** — a project's hard constraints (`local_only`, `allowed_harnesses`).
  A constraint REMOVES rows and never adjusts a score, so a strong
  preference cannot outvote it (D4).
- **Preferences** — a user's `defaults` (an ordered allowlist of harnesses), `weights` over
  four axes, and `excludes`. Taste, scored last (D3, D6).

`resolve()` runs the steps in a fixed order — rows for the tier → policy → profile →
installed → score → ties — and keeps every step's casualties, because a resolution nobody can
read is the hook-pack failure mode this design exists to avoid (D8). The server stores and
shows profiles and policy; it never calls this. Only the machine that will spawn knows what
it has installed.

Measured axes (`quality`, `latency`) arrive as a `Measured` map from whoever read the ledger.
Below `MIN_SAMPLE` finished attempts an axis contributes nothing and the explanation says
`unmeasured` (D7, D16): nothing here invents a quality number.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import adapters as adapters_mod

STATUSES = ("verified", "unverified", "failed", "unregistered")
LANES = ("frontend", "backend", "mixed", "any")
TIERS = ("cheap", "frontier")
AXES = ("cost", "quality", "latency", "locality")
COST_AXIS = {"local": 1.0, "cheap": 0.6, "frontier": 0.2}
#: D7: finished attempts before a measured axis is allowed to score.
MIN_SAMPLE = 5
DEFAULT_PATH = Path(__file__).with_name("matrix.toml")


class MatrixError(ValueError):
    """The matrix file says something it may not: a verified row with no evidence, an unknown
    status, a lane nobody defined. Refused at load so a bad row never reaches a spawn."""


LAYERS = ("project", "org", "platform", "prior")
COST_COVERAGE = 0.8
#: D20: linear from 1.0 at the target to 0.2 at twice the target, floored at 0.2.
BUDGET_FLOOR = 0.2


def _axis(*, value, n: int, source: str | None, used: bool, **extra) -> dict:
    """One scored axis: value plus where it came from and the count behind it (AC24)."""
    out = {"value": value, "n": n, "source": source, "used": used}
    out.update(extra)
    return out


@dataclass(frozen=True)
class Evidence:
    item: str
    date: str
    outcome: str
    note: str = ""
    #: PRD-41 D10: optional. Newest entry naming a capability is that capability's status.
    capability: str = ""
    status: str = ""


@dataclass(frozen=True)
class Row:
    harness: str
    model: str
    vendor: str
    lane: str
    tier: str
    status: str
    order: int
    cost_class: str
    local: bool
    evidence: tuple[Evidence, ...] = ()
    price_per_mtoken_in: float | None = None
    price_per_mtoken_out: float | None = None

    @property
    def key(self) -> str:
        return f"{self.harness}:{self.model}" if self.model else self.harness

    @property
    def latest(self) -> Evidence | None:
        return self.evidence[-1] if self.evidence else None

    def matches(self, tier: str, lane: str) -> bool:
        return self.tier == tier and self.lane in (lane, "any")

    def status_for(self, capability: str | None = None) -> str:
        """D10: newest evidence entry naming `capability`, else the row's status."""
        if not capability:
            return self.status
        for ev in reversed(self.evidence):
            if ev.capability == capability:
                if ev.status in STATUSES:
                    return ev.status
                if ev.outcome == "failed":
                    return "failed"
                if ev.outcome in ("signed_off", "verified", "review"):
                    return "verified"
                return self.status
        return self.status

    def prior_quality(self, capability: str) -> "Sample | None":
        st = self.status_for(capability)
        if st == "verified":
            return Sample(value=1.0, n=0, layer="prior", note="committed")
        if st == "failed":
            return Sample(value=0.0, n=0, layer="prior", note="committed")
        return None


@dataclass(frozen=True)
class Policy:
    """A project's hard constraints (D4). All off is no constraint."""
    local_only: bool = False
    allowed_harnesses: tuple[str, ...] = ()
    #: PRD-41 D20. A FILTER: expected tokens-to-sign-off over the remaining cap drops the row.
    caps: dict = field(default_factory=dict)

    @classmethod
    def of(cls, raw: dict | None) -> "Policy":
        raw = raw or {}
        caps = raw.get("caps") if isinstance(raw.get("caps"), dict) else {}
        kept = {k: caps[k] for k in ("per_attempt_tokens", "per_item_tokens",
                                     "per_period_tokens", "period") if k in caps}
        return cls(
            local_only=bool(raw.get("local_only")),
            allowed_harnesses=tuple(str(h) for h in (raw.get("allowed_harnesses") or [])),
            caps=kept,
        )


@dataclass(frozen=True)
class Profile:
    """A user's taste (D3). `defaults` is an ordered ALLOWLIST; its order is the second
    tiebreak (D6). A weight of 0 means indifferent, never exclusion — that is `excludes`."""
    user: str
    defaults: tuple[str, ...] = ()
    weights: dict = field(default_factory=dict)
    excludes: tuple[str, ...] = ()
    #: PRD-41 D20: soft per-sign-off target. None means rank-scaling (D16). Never a filter.
    budget_tokens: int | None = None

    @classmethod
    def of(cls, raw: dict | None) -> "Profile | None":
        if not raw:
            return None
        budget = raw.get("budget_tokens")
        try:
            budget_n = int(budget) if budget is not None else None
        except (TypeError, ValueError):
            budget_n = None
        return cls(
            user=str(raw.get("user") or raw.get("user_id") or "?"),
            defaults=tuple(str(h) for h in (raw.get("defaults") or [])),
            weights={k: float(v) for k, v in (raw.get("weights") or {}).items() if k in AXES},
            excludes=tuple(str(x) for x in (raw.get("excludes") or [])),
            budget_tokens=budget_n if budget_n and budget_n > 0 else None,
        )

    def normalised(self) -> dict:
        total = sum(v for v in self.weights.values() if v > 0)
        if total <= 0:
            return {}
        return {k: v / total for k, v in self.weights.items() if v > 0}


@dataclass(frozen=True)
class Sample:
    """A measured axis value with the count behind it (D7)."""
    value: float
    n: int
    layer: str = "project"
    n_band: str | None = None
    note: str = ""

    def clears_floor(self) -> bool:
        if self.n_band:
            return True
        if self.layer == "prior":
            return True
        return self.n >= MIN_SAMPLE


@dataclass(frozen=True)
class CostSample:
    """Tokens to a signed-off outcome (D16). Bounced attempts stay in the numerator."""
    tokens_to_signoff: float
    reported: int
    finished: int
    comparable: bool
    tokens_in: float = 0.0
    tokens_out: float = 0.0
    layer: str = "project"

    @property
    def reporting_share(self) -> float:
        return (self.reported / self.finished) if self.finished else 0.0


#: `{(vendor, model, lane, tier): {"quality": Sample, "latency": Sample}}` — read off
#: `fleet_status.measured` (PRD-37 D7). Keyed by VENDOR because that is what a child declares
#: (`capabilities.vendor`) and what the ledger can therefore attribute; the matrix row carries
#: its vendor, so the join is exact. Absent means unmeasured, never zero.
Measured = dict[tuple[str, str, str, str], dict[str, Sample]]

#: PRD-41 D5: `{(vendor, model, capability, layer): {quality, latency, cost}}`.
CapMeasured = dict[tuple[str, str, str, str], dict[str, object]]

#: `{(vendor, model, lane, tier): {"S": Sample, "M": Sample, "L": Sample}}` — the same cells
#: split by difficulty band (PRD-38 D9). NOT part of the resolution: the supervisor picks a
#: tier before it knows an item's band, so this is shown to a person and read by nobody else.
#: A harness handed doc items outscores one handed migrations, and the pooled rate cannot say
#: which happened.
Bands = dict[tuple[str, str, str, str], dict[str, Sample]]


def measured_of(rows: list[dict] | None) -> Measured:
    """The server's `measured` list as the lookup `resolve` reads. Cells with no latency
    carry quality alone; a `None` axis is simply absent.

    PRD-41 re-keyed the payload on capability. Old cells (lane × tier) still parse so a
    supervisor that has not yet passed `capabilities` into `resolve` keeps working; new
    cells are ignored here and read by `cap_measured_of`.
    """
    out: Measured = {}
    for r in rows or []:
        if r.get("capability") is not None:
            continue
        try:
            key = (str(r["vendor"]), str(r.get("model") or ""), str(r["lane"]), str(r["tier"]))
        except (KeyError, TypeError):
            continue
        cell: dict[str, Sample] = {}
        for axis in ("quality", "latency"):
            s = r.get(axis)
            if isinstance(s, dict) and s.get("value") is not None:
                cell[axis] = Sample(value=float(s["value"]), n=int(s.get("n") or 0))
        if cell:
            out[key] = cell
    return out


def cap_measured_of(rows: list[dict] | None) -> CapMeasured:
    """Capability-keyed cells with a layer per cell (D5)."""
    out: CapMeasured = {}
    for r in rows or []:
        cap = r.get("capability")
        if not cap:
            continue
        try:
            layer = str(r.get("layer") or "project")
            if layer not in LAYERS:
                layer = "project"
            key = (str(r["vendor"]), str(r.get("model") or ""), str(cap), layer)
        except (KeyError, TypeError):
            continue
        cell: dict[str, object] = {}
        for axis in ("quality", "latency"):
            s = r.get(axis)
            if isinstance(s, dict) and s.get("value") is not None:
                cell[axis] = Sample(
                    value=float(s["value"]), n=int(s.get("n") or 0), layer=layer,
                    n_band=str(r["n_band"]) if r.get("n_band") else None,
                    note=str(r.get("inherited_from") or ""),
                )
        cost = r.get("cost")
        if isinstance(cost, dict):
            cell["cost"] = CostSample(
                tokens_to_signoff=float(cost.get("tokens_to_signoff") or 0),
                reported=int(cost.get("reported") or 0),
                finished=int(cost.get("finished") or 0),
                comparable=bool(cost.get("comparable")),
                tokens_in=float(cost.get("tokens_in") or 0),
                tokens_out=float(cost.get("tokens_out") or 0),
                layer=layer,
            )
        if cell:
            out[key] = cell
    return out


def bands_of(rows: list[dict] | None) -> Bands:
    """The same cells' `bands` breakdown, for the doctor to PRINT (PRD-38 D9).

    Deliberately a second function and a second structure: `measured_of` feeds the resolver,
    and nothing that feeds the resolver should quietly gain a dimension the resolver cannot
    supply a value for.
    """
    out: Bands = {}
    for r in rows or []:
        try:
            key = (str(r["vendor"]), str(r.get("model") or ""), str(r["lane"]), str(r["tier"]))
        except (KeyError, TypeError):
            continue
        bands = r.get("bands")
        if not isinstance(bands, dict):
            continue
        cell = {str(name): Sample(value=float(v["value"]), n=int(v.get("n") or 0))
                for name, v in bands.items()
                if isinstance(v, dict) and v.get("value") is not None}
        if cell:
            out[key] = cell
    return out


@dataclass
class Resolution:
    """What happened at each step, so the reply and the log can say it (D8, D21)."""
    source: str
    tier: str
    lane: str
    profile: Profile | None
    eligible: dict = field(default_factory=dict)
    dropped: dict = field(default_factory=dict)
    scored: list = field(default_factory=list)
    #: The same drops as `dropped`, structurally (PRD-38 D7). The strings are for a person;
    #: these are what lets a REPLAY re-rank a resolution that already happened without
    #: re-running the resolver against a matrix that has moved since.
    dropped_rows: list = field(default_factory=list)
    winner: Row | None = None
    runner_up: Row | None = None
    refused: str = ""
    capabilities: list = field(default_factory=list)
    stages: list = field(default_factory=list)
    unenforceable_cap: dict | None = None

    def explain(self) -> dict:
        def row_out(entry) -> dict | None:
            if entry is None:
                return None
            row, score, axes = entry
            return {"harness": row.harness, "model": row.model, "status": row.status,
                    "score": round(score, 3), "axes": axes}
        def full(entry) -> dict:
            row, score, axes = entry
            return {"harness": row.harness, "model": row.model, "vendor": row.vendor,
                    "status": row.status, "score": round(score, 3), "order": row.order,
                    "local": row.local, "axes": axes}
        profile = "none"
        if self.profile:
            profile = {"user": self.profile.user, "defaults": list(self.profile.defaults),
                       "weights": self.profile.normalised()}
            if self.profile.budget_tokens:
                profile["budget_tokens"] = self.profile.budget_tokens
        out = {
            "source": self.source,
            "tier": self.tier, "lane": self.lane,
            "eligible": dict(self.eligible),
            "dropped": {k: list(v) for k, v in self.dropped.items() if v},
            # PRD-38 D7: everything a replay needs to re-rank THIS resolution under a proposed
            # change — every candidate's score and status, and every drop with the score it
            # would have had. Recorded facts, so a card speaks about resolutions that really
            # happened rather than simulating today's matrix over last month's work.
            "shortlist": [full(e) for e in self.scored],
            "dropped_rows": list(self.dropped_rows),
            "winner": row_out(next((s for s in self.scored if s[0] is self.winner), None)),
            "runner_up": row_out(next((s for s in self.scored if s[0] is self.runner_up), None)),
            "profile": profile,
            "refused": self.refused or None,
        }
        if self.capabilities:
            out["capabilities"] = list(self.capabilities)
        if self.stages:
            out["stages"] = list(self.stages)
        if self.unenforceable_cap:
            out["unenforceable_cap"] = dict(self.unenforceable_cap)
        return out


def _dropped_row(row: Row, stage: str, why: str, score: float) -> dict:
    """One dropped candidate, with the score it would have had (D15 in structured form)."""
    return {"harness": row.harness, "model": row.model, "vendor": row.vendor,
            "status": row.status, "score": round(score, 3), "order": row.order,
            "local": row.local, "stage": stage, "why": why}


@dataclass(frozen=True)
class Matrix:
    rows: tuple[Row, ...]
    path: Path

    def for_(self, tier: str, lane: str) -> list[Row]:
        return [r for r in self.rows if r.matches(tier, lane)]

    def resolve(
        self, *, tier: str, lane: str = "any",
        profile: Profile | None = None, policy: Policy | None = None,
        installed: Callable[[Row], tuple[bool, str]] | None = None,
        measured: Measured | None = None,
        capabilities: list[str] | None = None,
        cap_measured: CapMeasured | None = None,
        spend: dict | None = None,
    ) -> Resolution:
        """D5, in order. Every step records what it dropped and why.

        PRD-41 D21 adds capabilities, caps and the budget target without changing the
        shape: policy still filters, profile still scores, installed is still last.
        """
        policy = policy or Policy()
        res = Resolution(source="matrix", tier=tier, lane=lane, profile=profile,
                         capabilities=list(capabilities or []))
        rows = self.for_(tier, lane)
        res.eligible["matrix"] = len(rows)
        res.stages.append({"stage": "rows", "kept": len(rows)})
        if not rows:
            res.refused = f"the matrix has no row for tier {tier!r}, lane {lane!r}"
            return res

        def would_score(row: Row) -> float:
            if capabilities:
                return self._score_cap(row, profile, cap_measured, capabilities, spend, rows_for_cost=None)[0]
            return self._score(row, profile, measured, lane)[0]

        # 1. policy — a constraint removes; it is explained WITH the score it would have had
        #    (D15), so a user sees taste lose to a rule rather than see an absence.
        kept, dropped = [], []
        for r in rows:
            why = _policy_reason(r, policy)
            if why:
                score = would_score(r)
                dropped.append(f"{r.key} ({why}; would have scored {score:.2f})")
                res.dropped_rows.append(_dropped_row(r, "policy", why, score))
            else:
                kept.append(r)
        rows = kept
        res.dropped["policy"] = dropped
        res.eligible["after_policy"] = len(rows)
        if dropped:
            res.stages.append({"stage": "policy.allowed_or_local", "dropped": list(dropped)})
        if not rows:
            res.refused = "project policy removed every row"
            return res

        # 1b. D20 caps — a FILTER, after the other policy keys, before any score. An
        #     unreporting row is dropped with `tokens not reported`; if that empties the
        #     set the resolution is refused naming the cap (criterion 28).
        if policy.caps and capabilities:
            rows, cap_refused = self._apply_caps(
                rows, res, policy, profile, capabilities, cap_measured, spend, would_score)
            if cap_refused:
                return res
            res.eligible["after_policy"] = len(rows)
            if not rows:
                res.refused = "project policy removed every row"
                return res

        # 2. profile — the allowlist and excludes. No profile is NO filter (D14).
        if profile is not None:
            kept, dropped = [], []
            for r in rows:
                if profile.defaults and r.harness not in profile.defaults:
                    dropped.append(f"{r.key} (not in your defaults)")
                    res.dropped_rows.append(_dropped_row(
                        r, "profile", "not in your defaults", would_score(r)))
                elif r.harness in profile.excludes or r.key in profile.excludes:
                    dropped.append(f"{r.key} (in your excludes)")
                    res.dropped_rows.append(_dropped_row(
                        r, "profile", "in your excludes", would_score(r)))
                else:
                    kept.append(r)
            rows = kept
            res.dropped["profile"] = dropped
            if dropped:
                res.stages.append({"stage": "profile", "dropped": list(dropped)})
        res.eligible["after_profile"] = len(rows)
        if not rows:
            res.refused = "your profile's defaults or excludes removed every row"
            return res

        # 3. failed rows never spawn (D16); unverified ones may, and are marked.
        for r in rows:
            if r.status == "failed":
                res.dropped_rows.append(_dropped_row(
                    r, "failed", "marked failed", would_score(r)))
        rows = [r for r in rows if r.status != "failed"]
        res.eligible["after_failed"] = len(rows)
        if not rows:
            res.refused = "every remaining row is marked failed"
            return res

        # 4. installed — last, because "won on score but is not on this machine" is the
        #    most useful message to give (D5).
        if installed is not None:
            kept, dropped = [], []
            for r in rows:
                ok, why = installed(r)
                (kept if ok else dropped).append(r if ok else f"{r.key} ({why})")
                if not ok:
                    res.dropped_rows.append(_dropped_row(
                        r, "installed", why, would_score(r)))
            rows = kept
            res.dropped["installed"] = dropped
            if dropped:
                res.stages.append({"stage": "installed", "dropped": list(dropped)})
        res.eligible["after_installed"] = len(rows)
        if not rows:
            res.refused = "no eligible row is installed on this machine"
            return res

        # 5. score, then ties: verified > unverified, the user's defaults order, matrix order.
        if capabilities:
            scored = [(r, *self._score_cap(r, profile, cap_measured, capabilities, spend,
                                           rows_for_cost=rows)) for r in rows]
        else:
            scored = [(r, *self._score(r, profile, measured, lane)) for r in rows]

        def rank(entry):
            r, score, _ = entry
            verified = 1 if r.status == "verified" else 0
            pref = -(profile.defaults.index(r.harness)) if profile and r.harness in profile.defaults else 0
            return (score, verified, pref, -r.order)
        scored.sort(key=rank, reverse=True)
        res.scored = scored
        res.winner = scored[0][0]
        res.runner_up = scored[1][0] if len(scored) > 1 else None
        res.stages.append({"stage": "score",
                           "winner": res.explain()["winner"],
                           "runner_up": res.explain()["runner_up"]})
        return res

    @staticmethod
    def _score(row: Row, profile: Profile | None, measured: Measured | None,
               lane: str = "any") -> tuple[float, dict]:
        """A weighted sum over the axes the user weighted (D6). Unweighted profile: every axis
        counts equally, which is what "no preference" means. Measured axes below MIN_SAMPLE
        contribute nothing and say so (D7). The measured cell is the one for the lane being
        RESOLVED — a row for `any` lane resolved for `backend` reads the backend cell, never
        a pooled one; resolving with no lane named reads nothing and says unmeasured."""
        weights = profile.normalised() if profile else {}
        if not weights:
            weights = {a: 1.0 / len(AXES) for a in AXES}
        axes: dict = {
            "cost": COST_AXIS.get(row.cost_class, 0.0),
            "locality": 1.0 if row.local else 0.0,
        }
        cell_lane = lane if lane != "any" else row.lane
        m = (measured or {}).get((row.vendor, row.model, cell_lane, row.tier), {})
        for axis in ("quality", "latency"):
            s = m.get(axis)
            if s is None or s.n < MIN_SAMPLE:
                axes[axis] = {"value": None, "n": (s.n if s else 0), "used": False, "note": "unmeasured"}
            else:
                axes[axis] = {"value": round(s.value, 3), "n": s.n, "used": True}
        score = 0.0
        for axis, w in weights.items():
            v = axes.get(axis)
            if isinstance(v, dict):
                if v["used"]:
                    score += w * float(v["value"])
            else:
                score += w * float(v)
        return score, axes

    def _apply_caps(self, rows: list[Row], res: Resolution, policy: Policy,
                    profile: Profile | None, capabilities: list[str],
                    cap_measured: CapMeasured | None, spend: dict | None,
                    would_score) -> tuple[list[Row], bool]:
        """Filter by token caps (D20). Returns (kept, refused?)."""
        remaining = dict(policy.caps)
        item_left = None
        if remaining.get("per_item_tokens") is not None:
            spent = int((spend or {}).get("item_tokens") or 0)
            item_left = int(remaining["per_item_tokens"]) - spent
        attempt_cap = remaining.get("per_attempt_tokens")
        period_left = None
        if remaining.get("per_period_tokens") is not None:
            period_spent = int((spend or {}).get("period_tokens") or 0)
            period_left = int(remaining["per_period_tokens"]) - period_spent

        kept, dropped, unreporting = [], [], []
        for r in rows:
            expected, comparable, share = _expected_tokens(r, capabilities, cap_measured)
            if not comparable:
                why = "tokens not reported"
                dropped.append(f"{r.key} ({why})")
                res.dropped_rows.append(_dropped_row(r, "policy", why, would_score(r)))
                unreporting.append({"harness": r.harness, "model": r.model, "key": r.key,
                                    "reporting_share": share})
                continue
            why = None
            if item_left is not None and expected > item_left:
                why = f"per_item_tokens: {_tok(expected)} expected > {_tok(max(item_left, 0))} left"
            elif attempt_cap is not None and expected > attempt_cap:
                why = f"per_attempt_tokens: {_tok(expected)} expected > {_tok(attempt_cap)}"
            elif period_left is not None and expected > period_left:
                why = f"per_period_tokens: {_tok(expected)} expected > {_tok(max(period_left, 0))} left"
            if why:
                dropped.append(f"{r.key} ({why})")
                res.dropped_rows.append(_dropped_row(r, "policy", why, would_score(r)))
            else:
                kept.append(r)
        if dropped:
            res.dropped.setdefault("policy", []).extend(dropped)
            res.stages.append({"stage": "policy.caps", "dropped": list(dropped)})
        if not kept and unreporting and (item_left is not None or attempt_cap is not None
                                         or period_left is not None):
            cap_name = ("per_item_tokens" if item_left is not None
                        else "per_attempt_tokens" if attempt_cap is not None
                        else "per_period_tokens")
            res.refused = f"{cap_name}: no eligible row reports tokens"
            res.unenforceable_cap = {"cap": cap_name, "rows": unreporting}
            return [], True
        return kept, False

    def _score_cap(self, row: Row, profile: Profile | None, cap_measured: CapMeasured | None,
                   capabilities: list[str], spend: dict | None,
                   rows_for_cost: list[Row] | None) -> tuple[float, dict]:
        """Quality is the mean over the item's capabilities of the first layer that
        clears the floor (D5). Cost is cost_class refined by tokens-to-sign-off (D16),
        replaced by the D20 curve when budget_tokens is set."""
        weights = profile.normalised() if profile else {}
        if not weights:
            weights = {a: 1.0 / len(AXES) for a in AXES}
        quality, quality_axes = _quality_for(row, capabilities, cap_measured)
        cost_value, cost_axis = _cost_axis_for(
            row, capabilities, cap_measured, profile, rows_for_cost)
        latency_s, latency_axis = _latency_for(row, capabilities, cap_measured)
        axes = {
            "quality": quality_axes,
            "cost": cost_axis,
            "latency": latency_axis,
            # AC24: all four axes carry source and n. Locality is the row's declared
            # local flag, not a sample — n=1 names that it came from one row.
            "locality": _axis(value=1.0 if row.local else 0.0, n=1, source="row", used=True),
        }
        if row.price_per_mtoken_in is not None or row.price_per_mtoken_out is not None:
            expected, comparable, _ = _expected_tokens(row, capabilities, cap_measured)
            if comparable and expected is not None:
                spend_ccy = _currency_spend(row, expected, cap_measured, capabilities)
                if spend_ccy is not None:
                    axes["spend"] = spend_ccy
        score = 0.0
        for axis, w in weights.items():
            v = axes.get(axis)
            if isinstance(v, dict):
                if v.get("used"):
                    score += w * float(v["value"])
            elif v is not None:
                score += w * float(v)
        return score, axes


def _layer_source(layers: list[str | None]) -> str | None:
    named = [layer for layer in layers if layer]
    if not named:
        return None
    unique = set(named)
    return named[0] if len(unique) == 1 else "mixed"


def _quality_for(row: Row, capabilities: list[str],
                 cap_measured: CapMeasured | None) -> tuple[float | None, dict]:
    per = []
    used = []
    for cap in capabilities:
        picked = None
        for layer in LAYERS:
            if layer == "prior":
                continue
            cell = (cap_measured or {}).get((row.vendor, row.model, cap, layer)) or {}
            s = cell.get("quality")
            if isinstance(s, Sample) and s.clears_floor():
                picked = {"capability": cap, "value": round(s.value, 3), "n": s.n,
                          "layer": layer, "n_band": s.n_band, "used": True}
                break
        if picked is None:
            prior = row.prior_quality(cap)
            inherited = (cap_measured or {}).get((row.vendor, row.model, cap, "prior"))
            if inherited and isinstance(inherited.get("quality"), Sample):
                s = inherited["quality"]
                picked = {"capability": cap, "value": round(s.value, 3), "n": s.n,
                          "layer": "prior", "used": True,
                          "note": s.note or inherited.get("note") or ""}
            elif prior is not None:
                picked = {"capability": cap, "value": prior.value, "n": prior.n,
                          "layer": "prior", "used": True, "note": prior.note}
            else:
                picked = {"capability": cap, "value": None, "n": 0, "layer": None,
                          "used": False, "note": "unmeasured"}
        per.append(picked)
        if picked.get("used"):
            used.append(float(picked["value"]))
    source = _layer_source([c.get("layer") for c in per if c.get("used")])
    if used:
        mean = sum(used) / len(used)
        return mean, _axis(value=round(mean, 3), n=len(used), source=source, used=True,
                           by_capability=per,
                           note=f"{len(used)} of {len(capabilities)} capabilities")
    return None, _axis(value=None, n=0, source=source, used=False, note="unmeasured",
                       by_capability=per)


def _cost_sample_for(row: Row, capabilities: list[str],
                     cap_measured: CapMeasured | None) -> CostSample | None:
    samples = []
    for cap in capabilities:
        for layer in LAYERS:
            if layer == "prior":
                continue
            cell = (cap_measured or {}).get((row.vendor, row.model, cap, layer)) or {}
            c = cell.get("cost")
            if isinstance(c, CostSample):
                samples.append(c)
                break
    if not samples:
        return None
    # A row is comparable only when every capability that produced a cost cell is
    # comparable — mixing a silent vendor into a mean would hide the gap.
    if not all(s.comparable and s.reporting_share >= COST_COVERAGE for s in samples):
        reported = sum(s.reported for s in samples)
        finished = sum(s.finished for s in samples)
        return CostSample(tokens_to_signoff=0, reported=reported, finished=finished,
                          comparable=False)
    mean = sum(s.tokens_to_signoff for s in samples) / len(samples)
    tin = sum(s.tokens_in for s in samples) / len(samples)
    tout = sum(s.tokens_out for s in samples) / len(samples)
    return CostSample(tokens_to_signoff=mean, reported=sum(s.reported for s in samples),
                      finished=sum(s.finished for s in samples), comparable=True,
                      tokens_in=tin, tokens_out=tout)


def _expected_tokens(row: Row, capabilities: list[str],
                     cap_measured: CapMeasured | None) -> tuple[float | None, bool, float]:
    s = _cost_sample_for(row, capabilities, cap_measured)
    if s is None:
        return None, False, 0.0
    return (s.tokens_to_signoff if s.comparable else None), s.comparable, s.reporting_share


def _cost_axis_for(row: Row, capabilities: list[str], cap_measured: CapMeasured | None,
                   profile: Profile | None, rows_for_cost: list[Row] | None) -> tuple[float, dict]:
    class_value = COST_AXIS.get(row.cost_class, 0.0)
    sample = _cost_sample_for(row, capabilities, cap_measured)
    target = profile.budget_tokens if profile else None
    reported = sample.reported if sample else 0
    if sample is None or not sample.comparable:
        return class_value, _axis(value=class_value, n=reported, source="class", used=True,
                                  note="class", cost_class=row.cost_class)
    tokens = sample.tokens_to_signoff
    if target:
        # D20 curve: 1.0 at the target, 0.2 at twice the target, floored at 0.2.
        if tokens <= target:
            value = 1.0
        else:
            value = max(BUDGET_FLOOR, 1.0 - (1.0 - BUDGET_FLOOR) * (tokens - target) / target)
        extra = {"note": f"measured {int(round(tokens))}/sign-off",
                 "tokens_to_signoff": round(tokens, 1), "budget_tokens": target}
        return value, _axis(value=round(value, 3), n=reported, source="measured", used=True,
                            **extra)
    # Rank-scaling among comparable eligible rows (D16). Without the set, class.
    if not rows_for_cost:
        return class_value, _axis(value=class_value, n=reported, source="class", used=True,
                                  note="class", cost_class=row.cost_class,
                                  tokens_to_signoff=round(tokens, 1))
    comparable = []
    for other in rows_for_cost:
        exp, ok, _ = _expected_tokens(other, capabilities, cap_measured)
        if ok and exp is not None:
            comparable.append(exp)
    if len(comparable) < 2:
        value = 1.0
    else:
        lo, hi = min(comparable), max(comparable)
        if hi == lo:
            value = 1.0
        else:
            # cheapest 1.0, dearest 0.2
            value = 1.0 - 0.8 * (tokens - lo) / (hi - lo)
    return value, _axis(value=round(value, 3), n=reported, source="measured", used=True,
                        note=f"measured {int(round(tokens))}/sign-off",
                        tokens_to_signoff=round(tokens, 1))


def _latency_for(row: Row, capabilities: list[str],
                 cap_measured: CapMeasured | None) -> tuple[float | None, dict]:
    samples = []
    for cap in capabilities:
        for layer in LAYERS:
            if layer == "prior":
                continue
            cell = (cap_measured or {}).get((row.vendor, row.model, cap, layer)) or {}
            s = cell.get("latency")
            if isinstance(s, Sample) and s.clears_floor():
                samples.append(s)
                break
    if not samples:
        return None, _axis(value=None, n=0, source=None, used=False, note="unmeasured")
    mean = sum(s.value for s in samples) / len(samples)
    n = min(s.n for s in samples)
    source = _layer_source([s.layer for s in samples])
    return mean, _axis(value=round(mean, 3), n=n, source=source, used=True)


def _currency_spend(row: Row, tokens: float, cap_measured: CapMeasured | None,
                    capabilities: list[str]) -> dict | None:
    pin, pout = row.price_per_mtoken_in, row.price_per_mtoken_out
    if pin is None and pout is None:
        return None
    sample = _cost_sample_for(row, capabilities, cap_measured)
    tin = sample.tokens_in if sample and sample.tokens_in else tokens / 2
    tout = sample.tokens_out if sample and sample.tokens_out else tokens / 2
    total = 0.0
    if pin is not None:
        total += (tin / 1_000_000.0) * pin
    if pout is not None:
        total += (tout / 1_000_000.0) * pout
    return {"currency_per_signoff": round(total, 4),
            "price_per_mtoken_in": pin, "price_per_mtoken_out": pout}


def _tok(n: float) -> str:
    if abs(n) >= 1000:
        return f"{int(round(n / 1000))}k"
    return str(int(round(n)))


def _policy_reason(row: Row, policy: Policy) -> str:
    if policy.local_only and not row.local:
        return "local_only"
    if policy.allowed_harnesses and row.harness not in policy.allowed_harnesses:
        return "not in allowed_harnesses"
    return ""


def load(path: Path | None = None) -> Matrix:
    """Read and validate the matrix. Refuses a row that claims more than its evidence."""
    mat, _notes = load_with_notes(path)
    return mat


def load_with_notes(path: Path | None = None) -> tuple[Matrix, list[str]]:
    """Read and validate the matrix, returning deprecation notes alongside.

    S5 (GRPH-758): `role` is accepted and ignored for one release. A row carrying it loads;
    the note says so. The old `load()` still works and discards the notes.
    """
    path = Path(path) if path else DEFAULT_PATH
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    rows: list[Row] = []
    notes: list[str] = []
    for i, r in enumerate(raw.get("rows") or [], 1):
        try:
            status = str(r["status"])
            if status not in STATUSES:
                raise MatrixError(f"row {i}: status {status!r} is not one of {STATUSES}")
            if r.get("lane", "any") not in LANES or r["tier"] not in TIERS:
                raise MatrixError(f"row {i}: lane/tier outside {LANES}/{TIERS}")
            if "role" in r:
                notes.append(f"row {i}: `role` is deprecated and ignored (S5/GRPH-758); "
                             "the key is now harness × model × lane × tier")
            ev = tuple(Evidence(
                item=str(e["item"]), date=str(e["date"]), outcome=str(e["outcome"]),
                note=str(e.get("note", "")),
                capability=str(e.get("capability") or ""),
                status=str(e.get("status") or ""),
            ) for e in (r.get("evidence") or []))
            if status in ("verified", "failed") and not ev:
                raise MatrixError(f"row {i} ({r['harness']}:{r.get('model', '')}): status {status!r} "
                                  "needs at least one evidence entry naming the item")
            if status == "unregistered" and r["harness"] in adapters_mod.ADAPTERS:
                raise MatrixError(f"row {i}: {r['harness']!r} is registered; the row cannot say unregistered")
            if status != "unregistered" and r["harness"] not in adapters_mod.ADAPTERS:
                raise MatrixError(f"row {i}: {r['harness']!r} has no adapter in ADAPTERS; the row must say "
                                  "`status = \"unregistered\"` rather than read as usable")
            pin = r.get("price_per_mtoken_in")
            pout = r.get("price_per_mtoken_out")
            rows.append(Row(
                harness=str(r["harness"]), model=str(r.get("model", "")), vendor=str(r.get("vendor", "")),
                lane=str(r.get("lane", "any")), tier=str(r["tier"]), status=status,
                order=int(r.get("order", 99)), cost_class=str(r.get("cost_class", "frontier")),
                local=bool(r.get("local", False)), evidence=ev,
                price_per_mtoken_in=float(pin) if pin is not None else None,
                price_per_mtoken_out=float(pout) if pout is not None else None,
            ))
        except KeyError as exc:
            raise MatrixError(f"row {i}: missing {exc}") from None
    return Matrix(rows=tuple(rows), path=path), notes


def vendor_of(harness: str, matrix: "Matrix | None" = None) -> str:
    """The vendor a matrix row gives this harness, else the harness name itself."""
    rows = (matrix or load()).rows
    for r in rows:
        if r.harness == harness:
            return r.vendor
    return harness


def declaration(harness: str, model: str = "", tier: str | None = None,
                matrix: "Matrix | None" = None) -> dict:
    """What a spawned child must register as (GRPH-732): `vendor` from the matrix row,
    `model` only when one was NAMED (a vendor default is unknowable here — qwen replaces an
    unknown -m silently, so guessing would be a claim the binary does not enforce), `tier`
    as requested or as the one row for this harness+model says."""
    mat = matrix or load()
    out: dict = {"vendor": vendor_of(harness, mat)}
    if model:
        out["model"] = model
    if tier:
        out["tier"] = tier
    else:
        tiers = {r.tier for r in mat.rows if r.harness == harness and (not model or r.model == model)}
        if len(tiers) == 1:
            out["tier"] = tiers.pop()
    return out


def explicit_resolution(harness: str, model: str, *, lane: str = "any",
                        tier: str = "", matrix: "Matrix | None" = None) -> dict | None:
    """What the matrix says about a row somebody named OUTRIGHT (GRPH-772).

    An explicit `spawn(adapter=…)` resolves nothing, so PRD-37 D8 produces no explanation and
    the ledger records none — which means the server never learns that harness's matrix status
    and PRD-38's R1 can never fire on a harness only ever spawned by name. A row that is only
    ever chosen deliberately is exactly the kind that most needs promoting on evidence.

    So this records what IS true: the matrix's own view of the row that ran. The shortlist has
    ONE entry because there was one — nothing was ranked, nothing was dropped, and a replay
    over it correctly reports that no reordering could have changed it. `source` says
    `explicit` so nobody mistakes this for a choice the resolver made.

    None when the matrix has no row for that harness and model: an unregistered adapter is
    something we know nothing about, and inventing a status for it is the failure this whole
    module exists to avoid.
    """
    mat = matrix or load()
    rows = [r for r in mat.rows if r.harness == harness and (not model or r.model == model)]
    if not rows:
        return None
    row = rows[0]
    entry = {"harness": row.harness, "model": row.model, "vendor": row.vendor,
             "status": row.status, "score": None, "order": row.order, "local": row.local,
             "axes": {}}
    return {
        "source": "explicit",
        "tier": tier or row.tier, "lane": lane,
        "eligible": {"named": 1},
        "dropped": {},
        "shortlist": [entry],
        "dropped_rows": [],
        "winner": entry,
        "runner_up": None,
        "profile": "none",
        "refused": None,
    }


def unregistered_adapter_files() -> list[str]:
    """Adapter modules present on disk but absent from the registry — codex today. A fact the
    matrix must carry as a row (criterion 2), never as a silence."""
    here = Path(adapters_mod.__file__).parent
    stems = {p.stem for p in here.glob("*.py") if p.stem != "__init__"}
    # Registered adapters are matched by the MODULE that defines them, not by name: the
    # `cursor-agent` adapter lives in `cursor.py`.
    registered = {type(a).__module__.rsplit(".", 1)[-1] for a in adapters_mod.ADAPTERS.values()}
    helpers = {"cursor_stream"}
    return sorted(n.replace("_", "-") for n in stems - registered - helpers)


def installed_checker(binary_overrides: dict[str, str] | None = None) -> Callable[[Row], tuple[bool, str]]:
    """Resolve each row's adapter and, where the adapter can be asked, whether it serves the
    model. `None` from `known_models` means "cannot be asked", which is not "no" (D11)."""
    cache: dict[str, tuple[bool, str, object]] = {}

    def check(row: Row) -> tuple[bool, str]:
        if row.status == "unregistered":
            return False, "no adapter registered"
        if row.harness not in cache:
            try:
                resolved = adapters_mod.resolve(row.harness, binary=(binary_overrides or {}).get(row.harness))
                cache[row.harness] = (True, "", resolved)
            except adapters_mod.AdapterError as exc:
                cache[row.harness] = (False, str(exc).split("\n")[0][:120], None)
        ok, why, resolved = cache[row.harness]
        if not ok:
            return False, why
        if resolved is not None:
            # GRPH-805. Asked BEFORE the model listing, because a harness that cannot spawn
            # at all makes "does it serve this model" a question about nothing — and because
            # the listing's `None` ("cannot be asked") is exactly what used to swallow this.
            blocked = resolved.adapter.spawn_blocked(resolved.binary)
            if blocked:
                return False, blocked
        if row.model and resolved is not None:
            try:
                served = resolved.adapter.known_models(resolved.binary)
            except Exception:  # noqa: BLE001 — cannot be asked
                served = None
            if served is not None and row.model not in served:
                return False, f"model {row.model!r} not served"
        return True, ""
    return check


def _bands_for(bands: "Bands | None", row: Row) -> dict[str, Sample]:
    """The band breakdown for this row's cells, summed across lanes when the row spans them.

    Summed rather than listed per lane: the row line is already long, and the question a band
    answers ("was this measured on easy work?") is not lane-specific. `n` is what makes the
    sum honest — a band with one sample says so.
    """
    totals: dict[str, list[tuple[float, int]]] = {}
    for (vendor, model, lane, tier), cell in (bands or {}).items():
        if (vendor, model, tier) != (row.vendor, row.model, row.tier):
            continue
        if row.lane != "any" and lane != row.lane:
            continue
        for name, s in cell.items():
            totals.setdefault(name, []).append((s.value, s.n))
    out: dict[str, Sample] = {}
    for name, seen in totals.items():
        n = sum(c for _, c in seen)
        if n:
            out[name] = Sample(value=sum(v * c for v, c in seen) / n, n=n)
    return out


def _cells_for(measured: Measured | None, row: Row) -> dict[str, Sample]:
    """For the doctor's row line: every measured axis for this vendor/model/tier, labelled by
    lane when the row spans lanes. Shown, never pooled."""
    out: dict[str, Sample] = {}
    for (vendor, model, lane, tier), cell in (measured or {}).items():
        if (vendor, model, tier) != (row.vendor, row.model, row.tier):
            continue
        if row.lane != "any" and lane != row.lane:
            continue
        for axis, s in cell.items():
            out[f"{axis}/{lane}" if row.lane == "any" else axis] = s
    return out


def _cap_status_text(row: Row) -> str:
    named = [ev.capability for ev in row.evidence if ev.capability]
    if not named:
        return ""
    parts = []
    seen = []
    for cap in named:
        if cap in seen:
            continue
        seen.append(cap)
        parts.append(f"{cap}={row.status_for(cap)}")
    return " · caps " + ", ".join(parts) if parts else ""


def doctor_lines(matrix: Matrix, installed: Callable[[Row], tuple[bool, str]],
                 profile: Profile | None, policy: Policy | None,
                 measured: Measured | None = None,
                 bands: Bands | None = None,
                 cap_measured: CapMeasured | None = None) -> list[tuple[str, str, str]]:
    """(name, status, detail) per row, then per tier: what this machine resolves to (D11).
    A row whose harness is not installed on this machine is UNKNOWN with the reason, never a
    silent drop; a verified row with no adapter at all fails in load() (D17)."""
    out: list[tuple[str, str, str]] = []
    for r in matrix.rows:
        ok, why = installed(r)
        ev = r.latest
        ev_text = (f"{ev.item} {ev.date}" + (f", +{len(r.evidence) - 1} more" if len(r.evidence) > 1 else "")) if ev else "no evidence"
        m = _cells_for(measured, r)
        meas = ", ".join(f"{k} {v.value:.2f} (n={v.n}{'' if v.n >= MIN_SAMPLE else ', unmeasured'})" for k, v in m.items()) or "unmeasured"
        # PRD-38 D9: the same cells split by difficulty band, printed beside the pooled rate
        # so a reader can see whether a good number came from doc fixes. Shown, never scored.
        b = _bands_for(bands, r)
        band_text = " · bands " + ", ".join(
            f"{name} {v.value:.2f} (n={v.n})" for name, v in sorted(b.items())) if b else ""
        layer_bits = []
        for (vendor, model, cap, layer), cell in sorted((cap_measured or {}).items()):
            if (vendor, model) != (r.vendor, r.model):
                continue
            q = cell.get("quality")
            if isinstance(q, Sample):
                layer_bits.append(f"{cap}/{layer} {q.value:.2f} (n={q.n})")
        layer_text = " · layers " + ", ".join(layer_bits) if layer_bits else ""
        cap_text = _cap_status_text(r)
        detail = (f"{r.tier}/{r.lane} · {r.status}{cap_text} · {ev_text} · "
                  f"installed: {'yes' if ok else 'no — ' + why} · {meas}{band_text}{layer_text}")
        if not ok:
            # Not installed HERE is a fact about this machine, not about the row: UNKNOWN. A
            # verified row whose harness has no adapter at all is caught by load() (D17).
            out.append((f"matrix {r.key}", "UNKNOWN", ("not installed here — " if r.status == "verified" else "") + detail))
        elif r.status == "unregistered":
            out.append((f"matrix {r.key}", "UNKNOWN", detail))
        else:
            out.append((f"matrix {r.key}", "PASS", detail))
    for tier in TIERS:
        res = matrix.resolve(tier=tier, profile=profile, policy=policy,
                             installed=installed, measured=measured,
                             cap_measured=cap_measured)
        if res.winner is None:
            out.append((f"resolve {tier}", "UNKNOWN", f"nothing: {res.refused}"))
        else:
            w = res.explain()["winner"]
            out.append((f"resolve {tier}", "PASS",
                        f"{w['harness']}:{w['model']} ({w['status']}, score {w['score']}) · profile "
                        f"{res.profile.user if res.profile else 'none'} · dropped {sum(len(v) for v in res.dropped.values())}"))
    return out
