"""Rules import, D2 (GRPH-1950, PRD GRPH-P92): every finding lands in exactly one bucket.

Pure decision code: it files nothing. A reader (parser or skill) hands over a `Finding`; `classify`
says which bucket it belongs in and what to file, and refuses to say "proposal" for anything the
pipeline would refuse (`rule_proposals.propose`) or that would hold nothing.

- `proposal`: a tighter value on an existing library rule, or a custom rule on a primitive the
  server evaluates for custom rules (`custom_rules.EVALUATED`).
- `external`: real, enforced outside Graphban. Only a parser reading forge or CI configuration
  files one; prose never does. The label says it is configuration, not safety. When a primitive
  could also enforce it, `alongside` carries that proposal.
- `guidance`: real, but no primitive the server evaluates can express it. Names the gap.
- `nothing`: the finding would loosen a rule, or targets an invariant. Nothing is filed and
  `why` says so; loosening stays person-only.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.services import custom_rules, rule_pack
from app.services import rule_proposals as RP

BUCKETS = ("proposal", "external", "guidance", "nothing")
#: Readers that parse forge or CI configuration. The prose skill is not one of them.
PARSER_SOURCES = ("forge", "ci", "codeowners", "attributes")
SOURCE_KINDS = PARSER_SOURCES + ("prose",)
EXTERNAL_LABEL = "held outside Graphban, not verified"


@dataclass(frozen=True)
class Citation:
    file: str
    lines: str  # "12-18"
    commit: str
    quote: str = ""

    def as_dict(self) -> dict:
        return {"kind": "file", "ref": self.file, "lines": self.lines,
                "commit": self.commit, "quote": self.quote}


@dataclass(frozen=True)
class Finding:
    source_kind: str
    citation: Citation
    stage: str
    title: str
    #: Tighten an existing rule…
    rule_id: str | None = None
    #: …or draft a custom rule on a primitive.
    slug: str | None = None
    primitive: str | None = None
    params: dict = field(default_factory=dict)
    #: A requirement enforced outside Graphban (parser sources only).
    external: bool = False
    #: The primitive the requirement needs but the server does not evaluate (guidance's gap).
    gap: str | None = None
    #: The document says to relax something. Never filed.
    loosens: bool = False


@dataclass(frozen=True)
class Placed:
    bucket: str
    why: str
    proposal: dict | None = None
    external: dict | None = None
    guidance: dict | None = None


def _proposal(f: Finding) -> tuple[dict | None, str]:
    """The proposal this finding can file, or `(None, why not)`."""
    cites = [f.citation.as_dict()]
    if f.rule_id:
        if RP._PRIMITIVE_OF.get(f.rule_id) is None and not _pack_has(f.rule_id):
            return None, f"{f.rule_id} is not a library rule"
        return {"kind": "tighten", "rule_id": f.rule_id, "params": dict(f.params),
                "citations": cites}, ""
    if f.primitive and f.slug:
        if f.primitive not in rule_pack.PRIMITIVES:
            return None, f"primitive {f.primitive!r} does not exist"
        if f.primitive not in custom_rules.EVALUATED:
            return None, (f"primitive {f.primitive!r} is not evaluated for custom rules, "
                          "so a rule on it would hold nothing")
        return {"kind": "new_rule", "slug": f.slug, "primitive": f.primitive, "title": f.title,
                "params": dict(f.params), "citations": cites}, ""
    return None, "no library rule or evaluated primitive names it"


def _pack_has(rule_id: str) -> bool:
    pack = rule_pack.CURRENT
    return bool(pack and rule_id in pack.templates)


def classify(f: Finding) -> Placed:
    """Place one finding in exactly one bucket."""
    if f.source_kind not in SOURCE_KINDS:
        raise ValueError(f"source_kind must be one of {SOURCE_KINDS}")
    target = f.rule_id or f.primitive or ""
    if f.loosens:
        return Placed("nothing", "the source allows something looser; loosening a rule is "
                      "person-only, so nothing is filed")
    if target in RP.INVARIANTS:
        return Placed("nothing", f"{target} is an invariant ({RP.INVARIANTS[target]}); "
                      "it is not proposable")
    prop, why_not = _proposal(f)
    if f.external:
        if f.source_kind not in PARSER_SOURCES:
            # Prose can never claim outside enforcement; it falls through to guidance.
            return Placed("guidance", "prose cannot assert enforcement outside Graphban",
                          guidance=_guidance(f))
        return Placed("external", EXTERNAL_LABEL, proposal=prop,
                      external={"title": f.title, "label": EXTERNAL_LABEL,
                                "source": f.citation.as_dict()})
    if prop is not None:
        return Placed("proposal", "maps to a primitive the server evaluates", proposal=prop)
    return Placed("guidance", why_not, guidance=_guidance(f))


def _guidance(f: Finding) -> dict:
    return {"stage": f.stage, "label": f.gap or "no-primitive", "text": f.title,
            "citations": [f.citation.as_dict()]}
