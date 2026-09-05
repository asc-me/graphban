"""The tier table: what `cheap` and `frontier` mean on THIS machine (PRD-36 D6, D16).

A parent that typed `tier: cheap` on a delegation cannot say which binary runs it — the
adapter is a property of the machine the supervisor runs on, not of the ledger. So the
operator names the mapping at launch, `--tier cheap=gbagent:qwen3.6:35b-a3b-coding-mtp-det
--tier frontier=claude:opus`, and `spawn(tier=...)` is `spawn(adapter, model)` looked up
here. The supervisor still chooses nothing: it carries what the operator named.

Immutable for the life of the process (D16). Changing a model is a restart, and every spawn
reply names the adapter and model that actually ran, so a stale table is visible on the
first child rather than discovered from a bill.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class UnknownTier(ValueError):
    """`spawn(tier=...)` named a tier the operator did not map at launch."""


@dataclass(frozen=True)
class TierLane:
    adapter: str
    model: str = ""


@dataclass(frozen=True)
class TierTable:
    lanes: dict[str, TierLane] = field(default_factory=dict)

    @classmethod
    def parse(cls, specs: list[str] | None) -> "TierTable":
        """`name=adapter[:model]`, one per `--tier`. The model may itself contain colons
        (`qwen3.6:35b-a3b-coding-mtp-det`), so the split is on the FIRST colon only."""
        lanes: dict[str, TierLane] = {}
        for spec in specs or []:
            if "=" not in spec:
                raise ValueError(f"--tier {spec!r}: expected name=adapter[:model]")
            name, lane = spec.split("=", 1)
            name = name.strip()
            adapter, _, model = lane.strip().partition(":")
            if not name or not adapter:
                raise ValueError(f"--tier {spec!r}: expected name=adapter[:model]")
            lanes[name] = TierLane(adapter=adapter, model=model)
        return cls(lanes=lanes)

    def resolve(self, tier: str) -> TierLane:
        lane = self.lanes.get(tier)
        if lane is None:
            known = ", ".join(sorted(self.lanes)) or "none"
            raise UnknownTier(
                f"tier {tier!r} is not mapped on this supervisor (mapped: {known}); "
                f"start it with --tier {tier}=<adapter>[:<model>]"
            )
        return lane

    def describe(self) -> dict[str, dict[str, str]]:
        return {name: {"adapter": lane.adapter, "model": lane.model}
                for name, lane in sorted(self.lanes.items())}
