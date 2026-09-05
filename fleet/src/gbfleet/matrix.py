"""The preference matrix and the resolver (PRD-37 D2, D5–D8, D14–D19).

Three layers, three owners, and this module joins them at spawn time:

- **Facts** — `matrix.toml`, committed, one row per harness × model × lane × role × tier with a
  status and the items that proved it. Loaded by `load()`.
- **Policy** — a project's hard constraints (`local_only`, `allowed_harnesses`,
  `reviewer_cross_vendor`). A constraint REMOVES rows and never adjusts a score, so a strong
  preference cannot outvote it (D4).
- **Preferences** — a user's `defaults` (an ordered allowlist of harnesses), `weights` over
  four axes, and `excludes`. Taste, scored last (D3, D6).

`resolve()` runs the steps in a fixed order — rows for the tier and role → policy → profile →
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
ROLES = ("worker", "reviewer")
TIERS = ("cheap", "frontier")
AXES = ("cost", "quality", "latency", "locality")
COST_AXIS = {"local": 1.0, "cheap": 0.6, "frontier": 0.2}
#: D7: finished attempts before a measured axis is allowed to score.
MIN_SAMPLE = 5
DEFAULT_PATH = Path(__file__).with_name("matrix.toml")


class MatrixError(ValueError):
    """The matrix file says something it may not: a verified row with no evidence, an unknown
    status, a lane nobody defined. Refused at load so a bad row never reaches a spawn."""


@dataclass(frozen=True)
class Evidence:
    item: str
    date: str
    outcome: str
    note: str = ""


@dataclass(frozen=True)
class Row:
    harness: str
    model: str
    vendor: str
    lane: str
    role: str
    tier: str
    status: str
    order: int
    cost_class: str
    local: bool
    evidence: tuple[Evidence, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.harness}:{self.model}" if self.model else self.harness

    @property
    def latest(self) -> Evidence | None:
        return self.evidence[-1] if self.evidence else None

    def matches(self, tier: str, role: str, lane: str) -> bool:
        return self.tier == tier and self.role == role and self.lane in (lane, "any")


@dataclass(frozen=True)
class Policy:
    """A project's hard constraints (D4). All off is no constraint."""
    local_only: bool = False
    allowed_harnesses: tuple[str, ...] = ()
    reviewer_cross_vendor: bool = False

    @classmethod
    def of(cls, raw: dict | None) -> "Policy":
        raw = raw or {}
        return cls(
            local_only=bool(raw.get("local_only")),
            allowed_harnesses=tuple(str(h) for h in (raw.get("allowed_harnesses") or [])),
            reviewer_cross_vendor=bool(raw.get("reviewer_cross_vendor")),
        )


@dataclass(frozen=True)
class Profile:
    """A user's taste (D3). `defaults` is an ordered ALLOWLIST; its order is the second
    tiebreak (D6). A weight of 0 means indifferent, never exclusion — that is `excludes`."""
    user: str
    defaults: tuple[str, ...] = ()
    weights: dict = field(default_factory=dict)
    excludes: tuple[str, ...] = ()

    @classmethod
    def of(cls, raw: dict | None) -> "Profile | None":
        if not raw:
            return None
        return cls(
            user=str(raw.get("user") or raw.get("user_id") or "?"),
            defaults=tuple(str(h) for h in (raw.get("defaults") or [])),
            weights={k: float(v) for k, v in (raw.get("weights") or {}).items() if k in AXES},
            excludes=tuple(str(x) for x in (raw.get("excludes") or [])),
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


#: `{(harness, model, lane, tier): {"quality": Sample, "latency": Sample}}` — read from the
#: ledger by the caller. Absent means unmeasured.
Measured = dict[tuple[str, str, str, str], dict[str, Sample]]


@dataclass
class Resolution:
    """What happened at each step, so the reply and the log can say it (D8)."""
    source: str
    tier: str
    role: str
    lane: str
    profile: Profile | None
    eligible: dict = field(default_factory=dict)
    dropped: dict = field(default_factory=dict)
    scored: list = field(default_factory=list)
    winner: Row | None = None
    runner_up: Row | None = None
    refused: str = ""

    def explain(self) -> dict:
        def row_out(entry) -> dict | None:
            if entry is None:
                return None
            row, score, axes = entry
            return {"harness": row.harness, "model": row.model, "status": row.status,
                    "score": round(score, 3), "axes": axes}
        return {
            "source": self.source,
            "tier": self.tier, "role": self.role, "lane": self.lane,
            "eligible": dict(self.eligible),
            "dropped": {k: list(v) for k, v in self.dropped.items() if v},
            "winner": row_out(next((s for s in self.scored if s[0] is self.winner), None)),
            "runner_up": row_out(next((s for s in self.scored if s[0] is self.runner_up), None)),
            "profile": ({"user": self.profile.user, "defaults": list(self.profile.defaults),
                         "weights": self.profile.normalised()} if self.profile else "none"),
            "refused": self.refused or None,
        }


@dataclass(frozen=True)
class Matrix:
    rows: tuple[Row, ...]
    path: Path

    def for_(self, tier: str, role: str, lane: str) -> list[Row]:
        return [r for r in self.rows if r.matches(tier, role, lane)]

    def resolve(
        self, *, tier: str, role: str = "worker", lane: str = "any",
        profile: Profile | None = None, policy: Policy | None = None,
        installed: Callable[[Row], tuple[bool, str]] | None = None,
        measured: Measured | None = None,
        builder_vendor: str | None = None,
    ) -> Resolution:
        """D5, in order. Every step records what it dropped and why."""
        policy = policy or Policy()
        res = Resolution(source="matrix", tier=tier, role=role, lane=lane, profile=profile)
        rows = self.for_(tier, role, lane)
        res.eligible["matrix"] = len(rows)
        if not rows:
            res.refused = f"the matrix has no {role} row for tier {tier!r}, lane {lane!r}"
            return res

        # 1. policy — a constraint removes; it is explained WITH the score it would have had
        #    (D15), so a user sees taste lose to a rule rather than see an absence.
        kept, dropped = [], []
        for r in rows:
            why = _policy_reason(r, policy, role, builder_vendor)
            if why:
                dropped.append(f"{r.key} ({why}; would have scored {self._score(r, profile, measured)[0]:.2f})")
            else:
                kept.append(r)
        rows = kept
        res.dropped["policy"] = dropped
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
                elif r.harness in profile.excludes or r.key in profile.excludes:
                    dropped.append(f"{r.key} (in your excludes)")
                else:
                    kept.append(r)
            rows = kept
            res.dropped["profile"] = dropped
        res.eligible["after_profile"] = len(rows)
        if not rows:
            res.refused = "your profile's defaults or excludes removed every row"
            return res

        # 3. failed rows never spawn (D16); unverified ones may, and are marked.
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
            rows = kept
            res.dropped["installed"] = dropped
        res.eligible["after_installed"] = len(rows)
        if not rows:
            res.refused = "no eligible row is installed on this machine"
            return res

        # 5. score, then ties: verified > unverified, the user's defaults order, matrix order.
        scored = [(r, *self._score(r, profile, measured)) for r in rows]

        def rank(entry):
            r, score, _ = entry
            verified = 1 if r.status == "verified" else 0
            pref = -(profile.defaults.index(r.harness)) if profile and r.harness in profile.defaults else 0
            return (score, verified, pref, -r.order)
        scored.sort(key=rank, reverse=True)
        res.scored = scored
        res.winner = scored[0][0]
        res.runner_up = scored[1][0] if len(scored) > 1 else None
        return res

    @staticmethod
    def _score(row: Row, profile: Profile | None, measured: Measured | None) -> tuple[float, dict]:
        """A weighted sum over the axes the user weighted (D6). Unweighted profile: every axis
        counts equally, which is what "no preference" means. Measured axes below MIN_SAMPLE
        contribute nothing and say so (D7)."""
        weights = profile.normalised() if profile else {}
        if not weights:
            weights = {a: 1.0 / len(AXES) for a in AXES}
        axes: dict = {
            "cost": COST_AXIS.get(row.cost_class, 0.0),
            "locality": 1.0 if row.local else 0.0,
        }
        m = (measured or {}).get((row.harness, row.model, row.lane, row.tier), {})
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


def _policy_reason(row: Row, policy: Policy, role: str, builder_vendor: str | None) -> str:
    if policy.local_only and not row.local:
        return "local_only"
    if policy.allowed_harnesses and row.harness not in policy.allowed_harnesses:
        return "not in allowed_harnesses"
    if role == "reviewer" and policy.reviewer_cross_vendor and builder_vendor and row.vendor == builder_vendor:
        return f"reviewer_cross_vendor: same vendor as the builder ({builder_vendor})"
    return ""


def load(path: Path | None = None) -> Matrix:
    """Read and validate the matrix. Refuses a row that claims more than its evidence."""
    path = Path(path) if path else DEFAULT_PATH
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    rows: list[Row] = []
    for i, r in enumerate(raw.get("rows") or [], 1):
        try:
            status = str(r["status"])
            if status not in STATUSES:
                raise MatrixError(f"row {i}: status {status!r} is not one of {STATUSES}")
            if r.get("lane", "any") not in LANES or r["role"] not in ROLES or r["tier"] not in TIERS:
                raise MatrixError(f"row {i}: lane/role/tier outside {LANES}/{ROLES}/{TIERS}")
            ev = tuple(Evidence(item=str(e["item"]), date=str(e["date"]), outcome=str(e["outcome"]),
                                note=str(e.get("note", ""))) for e in (r.get("evidence") or []))
            if status in ("verified", "failed") and not ev:
                raise MatrixError(f"row {i} ({r['harness']}:{r.get('model', '')}): status {status!r} "
                                  "needs at least one evidence entry naming the item")
            if status == "unregistered" and r["harness"] in adapters_mod.ADAPTERS:
                raise MatrixError(f"row {i}: {r['harness']!r} is registered; the row cannot say unregistered")
            if status != "unregistered" and r["harness"] not in adapters_mod.ADAPTERS:
                raise MatrixError(f"row {i}: {r['harness']!r} has no adapter in ADAPTERS; the row must say "
                                  "`status = \"unregistered\"` rather than read as usable")
            rows.append(Row(
                harness=str(r["harness"]), model=str(r.get("model", "")), vendor=str(r.get("vendor", "")),
                lane=str(r.get("lane", "any")), role=str(r["role"]), tier=str(r["tier"]), status=status,
                order=int(r.get("order", 99)), cost_class=str(r.get("cost_class", "frontier")),
                local=bool(r.get("local", False)), evidence=ev,
            ))
        except KeyError as exc:
            raise MatrixError(f"row {i}: missing {exc}") from None
    return Matrix(rows=tuple(rows), path=path)


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
        if row.model and resolved is not None:
            try:
                served = resolved.adapter.known_models(resolved.binary)
            except Exception:  # noqa: BLE001 — cannot be asked
                served = None
            if served is not None and row.model not in served:
                return False, f"model {row.model!r} not served"
        return True, ""
    return check


def doctor_lines(matrix: Matrix, installed: Callable[[Row], tuple[bool, str]],
                 profile: Profile | None, policy: Policy | None,
                 measured: Measured | None = None) -> list[tuple[str, str, str]]:
    """(name, status, detail) per row, then per tier: what this machine resolves to (D11).
    A row whose harness is not installed on this machine is UNKNOWN with the reason, never a
    silent drop; a verified row with no adapter at all fails in load() (D17)."""
    out: list[tuple[str, str, str]] = []
    for r in matrix.rows:
        ok, why = installed(r)
        ev = r.latest
        ev_text = (f"{ev.item} {ev.date}" + (f", +{len(r.evidence) - 1} more" if len(r.evidence) > 1 else "")) if ev else "no evidence"
        m = (measured or {}).get((r.harness, r.model, r.lane, r.tier), {})
        meas = ", ".join(f"{k} {v.value:.2f} (n={v.n}{'' if v.n >= MIN_SAMPLE else ', unmeasured'})" for k, v in m.items()) or "unmeasured"
        detail = f"{r.role}/{r.tier}/{r.lane} · {r.status} · {ev_text} · installed: {'yes' if ok else 'no — ' + why} · {meas}"
        if not ok:
            # Not installed HERE is a fact about this machine, not about the row: UNKNOWN. A
            # verified row whose harness has no adapter at all is caught by load() (D17).
            out.append((f"matrix {r.key}", "UNKNOWN", ("not installed here — " if r.status == "verified" else "") + detail))
        elif r.status == "unregistered":
            out.append((f"matrix {r.key}", "UNKNOWN", detail))
        else:
            out.append((f"matrix {r.key}", "PASS", detail))
    for tier in TIERS:
        for role in ROLES:
            res = matrix.resolve(tier=tier, role=role, profile=profile, policy=policy,
                                 installed=installed, measured=measured)
            if res.winner is None:
                out.append((f"resolve {role}/{tier}", "UNKNOWN", f"nothing: {res.refused}"))
            else:
                w = res.explain()["winner"]
                out.append((f"resolve {role}/{tier}", "PASS",
                            f"{w['harness']}:{w['model']} ({w['status']}, score {w['score']}) · profile "
                            f"{res.profile.user if res.profile else 'none'} · dropped {sum(len(v) for v in res.dropped.values())}"))
    return out
