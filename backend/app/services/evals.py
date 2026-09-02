"""Golden-set evals for generative surfaces (GRPH-224).

Deterministic tests cover code. They do not cover what an extractor *says*. This
module is the missing harness: curated fixtures, mechanical shape checks that
always run, and an LLM-as-judge that is explicitly ungraded when the stub is
what this instance has.

An ungraded run is not a pass. The suite status is `ok` (mechanical held, judge
not asked or agreed), `failed`, or `absent` (nobody looked — missing cases dir,
or zero files). `pass` is not a word this report uses for the suite, because it
is the reassuring reading of an empty judge.

The judge reuses `resolve_role(..., "memory.judge")`, which inherits the
project's chat provider until a role override is set (GRPH-316). The rubric is groundedness against
the fixture, not "is this a good lesson?" — that question is how GRPH-358's
false lesson would have scored well.

`generate_digest` is not a surface: it is a template, not a model call.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app.evals import CASES_DIR
from app.services import agent_chat as agent_chat_svc
from app.services import insights as insights_svc
from app.services import items as items_svc
from app.services import memory as mem_svc
from app.services import platform as platform_svc
from app.services import prds as prd_svc

logger = logging.getLogger(__name__)

# Closed set. The CLI spells the same names out rather than importing this on
# `--help` (SQLAlchemy); `test_evals` pins the two lists against each other.
SURFACES = ("extract_lessons", "grill_prd", "assistant", "prd_eval")

# Same count and rationale as `memory.JUDGE_SAMPLES` (GRPH-348): a single
# temperature-0 sample is not an adjudication. Unanimity on `grounded`, not a
# majority — a split is ungraded, not a coin flip reported as a verdict.
JUDGE_SAMPLES = 3

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)

_JUDGE_SYSTEM = (
    "You score one extractor run against a fixture. You are not writing lessons. "
    "A shard is UNGROUNDED if it states any forbidden claim as settled fact "
    "(paraphrase counts). Respond with ONLY a compact JSON object: "
    '{"grounded": <true|false>, "relevant": <true|false>, '
    '"format_ok": <true|false>, "reason": "<one short sentence>"}'
)
_JUDGE_QUESTION = "Score this extractor run. Return only the JSON object."


class UnknownSurface(ValueError):
    """A surface nobody runs. Raised rather than returning an empty-ok suite —
    `surface=extract_lesson` (singular) must not look like everything passed."""


def cases_dir() -> Path:
    return CASES_DIR


def load_cases(root: Path | None = None, *, surface: str | None = None) -> list[dict] | None:
    """Load JSON fixtures. `None` means the tree was absent or empty — not `[]`.

    `[]` would be the clean reading of "we looked and there is nothing to
    score." The suite then reports `absent` so a deleted cases directory cannot
    green the eval job.
    """
    base = root if root is not None else cases_dir()
    if not base.is_dir():
        return None
    names = SURFACES if surface is None else (surface,)
    found: list[dict] = []
    for name in names:
        folder = base / name
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (ValueError, OSError) as e:
                raise ValueError(f"eval case {path} is not valid JSON ({e})") from e
            if not isinstance(data, dict) or not data.get("id"):
                raise ValueError(f"eval case {path} needs an id")
            data.setdefault("surface", name)
            data["_path"] = str(path)
            found.append(data)
    return found or None


def _texts(shards: list[dict]) -> list[str]:
    out = []
    for s in shards:
        if isinstance(s, dict):
            out.append(str(s.get("text") or ""))
        else:
            out.append(str(s))
    return out


def _blob(texts: list[str]) -> str:
    return "\n".join(texts).lower()


def _mechanical(case: dict, shards: list[dict]) -> dict:
    """Shape checks that do not need a model. Failures are named, not counted."""
    expect = case.get("expect") or {}
    failures: list[str] = []
    n = len(shards)
    min_n = expect.get("min_shards")
    max_n = expect.get("max_shards")
    if min_n is not None and n < int(min_n):
        failures.append(f"got {n} shards, min {min_n}")
    if max_n is not None and n > int(max_n):
        failures.append(f"got {n} shards, max {max_n}")
    want_status = expect.get("status")
    want_origin = expect.get("origin")
    for i, s in enumerate(shards):
        if not isinstance(s, dict):
            failures.append(f"shard {i} is not an object")
            continue
        if want_status and s.get("status") != want_status:
            failures.append(f"shard {i} status {s.get('status')!r} != {want_status!r}")
        if want_origin and s.get("origin") != want_origin:
            failures.append(f"shard {i} origin {s.get('origin')!r} != {want_origin!r}")
    blob = _blob(_texts(shards))
    for needle in expect.get("must_contain") or []:
        if needle.lower() not in blob:
            failures.append(f"missing {needle!r}")
    for needle in expect.get("must_not_contain") or []:
        if needle.lower() in blob:
            failures.append(f"forbidden {needle!r}")
    extras = shards[0] if shards and isinstance(shards[0], dict) else {}
    if "grill_complete" in expect and extras.get("complete") is not expect["grill_complete"]:
        failures.append(
            f"complete {extras.get('complete')!r} != {expect['grill_complete']!r}"
        )
    if "eval_mechanical_ready" in expect and extras.get("complete") is not expect["eval_mechanical_ready"]:
        failures.append(
            f"mechanical_ready {extras.get('complete')!r} != {expect['eval_mechanical_ready']!r}"
        )
    if "eval_missing" in expect:
        got = list(extras.get("missing") or [])
        want = list(expect["eval_missing"])
        if sorted(got) != sorted(want):
            failures.append(f"missing {got} != {want}")
    if "grill_graded" in expect and extras.get("graded") is not expect["grill_graded"]:
        failures.append(
            f"grill graded {extras.get('graded')!r} != {expect['grill_graded']!r}"
        )
    if "grill_answers" in expect and extras.get("answers") != expect["grill_answers"]:
        failures.append(
            f"answers {extras.get('answers')!r} != {expect['grill_answers']!r}"
        )
    if "deferred" in expect:
        got = list(extras.get("deferred") or [])
        want = list(expect["deferred"])
        if sorted(got) != sorted(want):
            failures.append(f"deferred {got} != {want}")
    needle = expect.get("ungraded_reason_contains")
    if needle:
        reason = str(extras.get("ungraded_reason") or "")
        if needle.lower() not in reason.lower():
            failures.append(f"ungraded_reason missing {needle!r} (got {reason!r})")
    return {"passed": not failures, "failures": failures}


def _parse_judge(raw: str) -> dict | None:
    if not raw:
        return None
    match = _JSON_OBJ_RE.search(raw)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or "grounded" not in data:
        return None
    return {
        "grounded": bool(data["grounded"]),
        "relevant": bool(data.get("relevant", True)),
        "format_ok": bool(data.get("format_ok", True)),
        "reason": str(data.get("reason") or "").strip(),
    }


def _judge_context(case: dict, texts: list[str]) -> str:
    expect = case.get("expect") or {}
    forbidden = expect.get("must_not_contain") or []
    src = case.get("input") or {}
    if (case.get("surface") or "") == "grill_prd":
        return "\n\n".join([
            "PRD TITLE: " + str(src.get("title") or ""),
            "PRD BODY:",
            str(src.get("body") or "(none)"),
            "GENERATED GRILL QUESTIONS:",
            "\n".join(f"- {t}" for t in texts) or "(none)",
            "FORBIDDEN IN THE QUESTIONS:",
            "\n".join(f"- {n}" for n in forbidden) or "(none named)",
        ])
    if (case.get("surface") or "") == "prd_eval":
        return "\n\n".join([
            "PRD TITLE: " + str(src.get("title") or ""),
            "PRD BODY:",
            str(src.get("body") or "(none)"),
            "APPROVAL-EVAL CALLOUTS:",
            "\n".join(f"- {t}" for t in texts) or "(none)",
            "FORBIDDEN IN THE CALLOUTS:",
            "\n".join(f"- {n}" for n in forbidden) or "(none named)",
        ])
    if (case.get("surface") or "") == "assistant":
        return "\n\n".join([
            "QUESTION:",
            str(src.get("question") or ""),
            "SEEDED MEMORY (the answer must be grounded here):",
            "\n".join(f"- {m}" for m in (src.get("memory") or [])) or "(none)",
            "ASSISTANT REPLY:",
            "\n".join(texts) or "(none)",
            "FORBIDDEN IN THE REPLY:",
            "\n".join(f"- {n}" for n in forbidden) or "(none named)",
        ])
    return "\n\n".join([
        "ITEM TITLE: " + str(src.get("title") or ""),
        "ITEM DESCRIPTION (proposal, possibly stale):",
        str(src.get("description") or "(none)"),
        "EVIDENCE (authoritative when present):",
        json.dumps(src.get("evidence") or [], ensure_ascii=False),
        "EXTRACTED SHARDS:",
        "\n".join(f"- {t}" for t in texts) or "(none)",
        "FORBIDDEN AS SETTLED FACT:",
        "\n".join(f"- {n}" for n in forbidden) or "(none named)",
    ])


def _judge(db: Session, case: dict, shards: list[dict], *, project_id: str) -> dict:
    """Ask the project's chat model. Stub / split / unusable → ungraded, never a fail.

    A fail is the judge agreeing the output is ungrounded. "We could not ask"
    is a different sentence and must not look like the extractor did well.
    """
    try:
        resolved = platform_svc.resolve_role(db, project_id, "memory.judge")
        provider, chat = resolved.provider_id, resolved.chat
    except Exception:  # noqa: BLE001 — a broken resolver is ungraded, not a crash
        logger.exception("evals judge: provider resolution failed")
        return {"outcome": "ungraded", "reason": "the chat provider could not be resolved"}
    if provider == "stub":
        return {
            "outcome": "ungraded",
            "reason": "stub cannot judge substance",
            "provider": "stub",
        }

    texts = _texts(shards)
    context = _judge_context(case, texts)
    verdicts: list[dict] = []
    from app.providers import llm_meter
    with llm_meter.llm_context(feature="evals.judge", project_id=project_id):
        for _ in range(JUDGE_SAMPLES):
            try:
                raw = chat.chat(system=_JUDGE_SYSTEM, context=context,
                                question=_JUDGE_QUESTION, temperature=0)
            except Exception:  # noqa: BLE001 — outage is ungraded
                logger.exception("evals judge: chat call failed")
                return {"outcome": "ungraded", "reason": "the judge could not be reached",
                        "provider": provider}
            parsed = _parse_judge(raw)
            if parsed is None:
                return {"outcome": "ungraded",
                        "reason": "the judge did not answer in the required form",
                        "provider": provider, "raw": (raw or "")[:240]}
            if verdicts and parsed["grounded"] != verdicts[0]["grounded"]:
                return {
                    "outcome": "ungraded",
                    "reason": "the judge did not agree with itself across samples",
                    "provider": provider,
                }
            verdicts.append(parsed)

    grounded = verdicts[0]["grounded"]
    return {
        "outcome": "pass" if grounded else "fail",
        "provider": provider,
        "grounded": grounded,
        "relevant": verdicts[0]["relevant"],
        "format_ok": verdicts[0]["format_ok"],
        "reason": verdicts[0]["reason"],
        "samples": len(verdicts),
    }


def _run_extract_lessons(db: Session, case: dict) -> tuple[list[dict], str]:
    """Create the fixture item and call the real extractor. Returns (shards, project_id).

    Evidence is appended through `update_item` so the CALL is the same write an
    operator uses; we do not complete the item, because completion auto-extracts
    and a second `extract_lessons` would duplicate shards and the forbidden-claim
    check would then pass-or-fail for the wrong reason.

    Shards are read back from the table, not from `extract_lessons`'s return
    dict — that dict omits `origin`, and a runner that trusted it would pass an
    origin check the write never made.
    """
    from sqlalchemy import select

    from app.models import MemoryShard

    src = case.get("input") or {}
    project_id = src.get("project_id") or "core"
    item = items_svc.create_item(
        db,
        title=str(src.get("title") or case["id"]),
        description=str(src.get("description") or ""),
        project_id=project_id,
        tags=["eval"],
    )
    evidence = src.get("evidence") or []
    if evidence:
        items_svc.update_item(db, item.id, evidence=evidence)
    insights_svc.extract_lessons(db, item.id)
    rows = list(db.scalars(
        select(MemoryShard).where(MemoryShard.source == f"lesson from {item.id}")
    ))
    shards = [{"id": r.id, "text": r.text, "status": r.status, "origin": r.origin}
              for r in rows]
    return shards, project_id


def _grill_outputs(text: str, done: dict) -> list[dict]:
    """One dict per question bullet, extras on the first so mechanical can see them.

    Splitting the bullets is load-bearing: a single blob would make `min_shards: 4`
    pass on any non-empty string, including a runner that never called `ai_command`.
    """
    bullets = [ln[2:].strip() for ln in (text or "").splitlines() if ln.startswith("- ")]
    rows = [{"text": b} for b in bullets] or [{"text": text or ""}]
    rows[0] = {
        **rows[0],
        "complete": bool(done.get("complete")),
        "graded": bool(done.get("graded")),
        "answers": int(done.get("answers") or 0),
        "deferred": list(done.get("deferred") or []),
        "outstanding": list(done.get("outstanding") or []),
        "ungraded_reason": str(done.get("ungraded_reason") or ""),
    }
    return rows


def _run_grill_prd(db: Session, case: dict) -> tuple[list[dict], str]:
    """Create the fixture PRD and call the real grill. Returns (outputs, project_id).

    `action=questions` is `ai_command_detail(..., "grill")` — the CALL `grill_prd`
    makes. It must not record answers: a green questions eval that also classified
    four dummy replies would approve the fixture, which is the theatre this pin
    exists to catch.

    `action=classify` records the fixture answers (if any) and calls
    `classify_grill`. Deferred dimensions are written first so the guard in
    classify is the thing under test, not a helper.
    """
    src = case.get("input") or {}
    project_id = src.get("project_id") or "core"
    prd = prd_svc.create_prd(
        db,
        title=str(src.get("title") or case["id"]),
        body=str(src.get("body") or ""),
        project_id=project_id,
    )
    action = case.get("action") or "questions"
    if action == "classify":
        for dim in src.get("defer") or []:
            prd_svc.set_dimension(db, prd.id, dim, "deferred",
                                  note="eval fixture", graded_by="eval")
        history = [{"role": "user", "text": a} for a in (src.get("answers") or []) if a]
        if history:
            prd_svc.record_grill_turns(db, prd.id, history, via="eval")
        done = prd_svc.classify_grill(db, prd)
        text = json.dumps({
            "complete": done.get("complete"),
            "graded": done.get("graded"),
            "deferred": done.get("deferred"),
            "outstanding": done.get("outstanding"),
            "ungraded_reason": done.get("ungraded_reason"),
        }, sort_keys=True)
        return _grill_outputs(text, done), project_id

    questions, _retried = prd_svc.ai_command_detail(db, prd.id, "grill")
    done = prd_svc.completion(db, prd.id)
    return _grill_outputs(questions, done), project_id


def _run_assistant(db: Session, case: dict) -> tuple[list[dict], str]:
    """Seed fixture memory and call the real `/api/agent/chat` service.

    Memory is published — search_memory hides candidates, and a fixture that
    stayed in the queue would make every grounded case fail for the wrong
    reason. The CALL is `agent_chat.reply`, not a composed string.
    """
    src = case.get("input") or {}
    project_id = src.get("project_id") or "core"
    for text in src.get("memory") or []:
        mem_svc.add_memory(
            db, text_body=str(text), scope="global", project_id=project_id,
            status="published", origin="user:eval", source="eval fixture",
        )
    out = agent_chat_svc.reply(
        db, project_id=project_id, message=str(src.get("question") or ""),
    )
    return [{"text": out["reply"]}], project_id


def _run_prd_eval(db: Session, case: dict) -> tuple[list[dict], str]:
    """Create the fixture PRD and call `approval_eval`. Returns (outputs, project_id).

    Mechanical only (`judge=False`): the harness's own LLM-as-judge scores the
    callouts. Asking the approval judge here would nest two models and make a
    stub suite fail for the wrong reason.

    Optional `input.items` seed linked work so a coverage-gap case can prove
    the CALL to `coverage()` rather than a helper that invents gaps.
    """
    src = case.get("input") or {}
    project_id = src.get("project_id") or "core"
    # `body is not None` wins over the template in create_prd, so omit it when
    # the fixture wants the standard skeleton (the thin-placeholder case).
    kw: dict = {
        "title": str(src.get("title") or case["id"]),
        "project_id": project_id,
        "template": str(src.get("template") or "blank"),
    }
    if "body" in src:
        kw["body"] = str(src.get("body") or "")
    prd = prd_svc.create_prd(db, **kw)
    for it in src.get("items") or []:
        items_svc.create_item(
            db,
            title=str(it.get("title") or "eval item"),
            project_id=project_id,
            prd_id=prd.id,
            prd_section=str(it.get("section") or ""),
        )
    report = prd_svc.approval_eval(db, prd, judge=False)
    callouts = list(report.get("callouts") or [])
    rows = [{"text": c} for c in callouts] or [{"text": report.get("coverage_note") or "(none)"}]
    rows[0] = {
        **rows[0],
        "complete": bool(report.get("mechanical_ready")),
        "ready": report.get("ready"),
        "judged": bool(report.get("judged")),
        "ungraded_reason": str(report.get("ungraded_reason") or ""),
        "missing": list(report.get("missing") or []),
    }
    return rows, project_id


_RUNNERS: dict[str, Callable] = {
    "extract_lessons": _run_extract_lessons,
    "grill_prd": _run_grill_prd,
    "assistant": _run_assistant,
    "prd_eval": _run_prd_eval,
}


def run_case(db: Session, case: dict, *, judge: bool = False) -> dict:
    surface = case.get("surface") or ""
    runner = _RUNNERS.get(surface)
    if runner is None:
        return {
            "id": case.get("id"),
            "surface": surface,
            "invoked": False,
            "outcome": "ungraded",
            "ungraded_reason": f"no runner for surface {surface!r}",
            "mechanical": {"passed": False, "failures": ["no runner"]},
            "judge_outcome": "ungraded",
            "graded": False,
        }

    shards, project_id = runner(db, case)
    mechanical = _mechanical(case, shards)
    judge_result = None
    if judge:
        judge_result = _judge(db, case, shards, project_id=project_id)
        judge_outcome = judge_result["outcome"]
    else:
        judge_outcome = "ungraded"
        judge_result = {"outcome": "ungraded", "reason": "judge not requested"}

    if not mechanical["passed"] or judge_outcome == "fail":
        outcome = "fail"
    else:
        outcome = "ok"

    return {
        "id": case["id"],
        "surface": surface,
        "invoked": True,
        "output_count": len(shards),
        "outputs": _texts(shards),
        "mechanical": mechanical,
        "graded": judge_outcome in ("pass", "fail"),
        "judge_outcome": judge_outcome,
        "judge": judge_result,
        "outcome": outcome,
    }


def run(db: Session, *, surface: str | None = None, judge: bool = False,
        root: Path | None = None) -> dict:
    """Run every matching case. `surface=None` means all registered surfaces."""
    if surface is not None and surface not in SURFACES:
        raise UnknownSurface(
            f"unknown eval surface {surface!r}; known: {', '.join(SURFACES)}"
        )
    cases = load_cases(root, surface=surface)
    if cases is None:
        return {
            "status": "absent",
            "graded": False,
            "reason": "no eval cases found — the suite did not look at any output",
            "cases": 0,
            "mechanical_passed": 0,
            "mechanical_failed": 0,
            "judge_passed": 0,
            "judge_failed": 0,
            "judge_ungraded": 0,
            "results": [],
        }

    results = [run_case(db, case, judge=judge) for case in cases]
    mech_fail = sum(1 for r in results if not r["mechanical"]["passed"])
    mech_pass = sum(1 for r in results if r["mechanical"]["passed"])
    j_pass = sum(1 for r in results if r["judge_outcome"] == "pass")
    j_fail = sum(1 for r in results if r["judge_outcome"] == "fail")
    j_ungraded = sum(1 for r in results if r["judge_outcome"] == "ungraded")
    failed = mech_fail > 0 or j_fail > 0
    return {
        "status": "failed" if failed else "ok",
        "graded": bool(results) and j_ungraded == 0 and j_fail == 0,
        "cases": len(results),
        "mechanical_passed": mech_pass,
        "mechanical_failed": mech_fail,
        "judge_passed": j_pass,
        "judge_failed": j_fail,
        "judge_ungraded": j_ungraded,
        "results": results,
    }


# ---- live human-eval sampling (GRPH-644) -------------------------------------
# Spans do not hold transcripts (GRPH-225). Sampling a row with no text would
# put un-labelable telemetry in Memory review, which is the absence rule
# wearing a queue. `output_preview` is the smallest payload that makes a span
# reviewable. Stub output is skipped: labelling the offline heuristic is not a
# live eval. Promote prints JSON; it does not write the repo.

SAMPLE_FEATURES = (
    "lessons.extract",
    "grill.classify",
    "memory.judge",
    "evals.judge",
)
SAMPLE_ORIGIN = "agent:eval-sample"


class UnlabelledSample(ValueError):
    """Promote was asked to freeze a candidate. Unlabelled is ungraded, not a case."""


def _span_source(span_id: int) -> str:
    return f"eval-span:{span_id}"


def _sample_text(span) -> str:
    preview = (span.output_preview or "").strip()
    model = f"{span.provider}/{span.model}" if span.model else span.provider
    project = span.project_id or "unattributed"
    return (
        f"Eval sample · {span.feature} · {model}\n"
        f"span {span.id} · project {project}\n\n"
        f"{preview}"
    )


def sample(db: Session, *, limit: int = 20, project_id: str | None = None) -> dict:
    """Copy recent labelled-able spans into Memory review as candidates.

    Idempotent on span id (`source=eval-span:N`). Never publishes. Returns counts
    so a zero is "nothing eligible", not a clean pass — see `labels`.
    """
    from sqlalchemy import select

    from app.models import LlmCallSpan, MemoryShard

    if limit < 1:
        raise ValueError("limit must be >= 1")
    stmt = (
        select(LlmCallSpan)
        .where(
            LlmCallSpan.feature.in_(SAMPLE_FEATURES),
            LlmCallSpan.ok.is_(True),
        )
        .order_by(LlmCallSpan.ts.desc())
    )
    if project_id:
        stmt = stmt.where(LlmCallSpan.project_id == project_id)
    spans = list(db.scalars(stmt.limit(max(limit * 8, 40))))
    sampled = 0
    skipped_stub = 0
    skipped_no_preview = 0
    skipped_already = 0
    created_ids: list[str] = []
    for span in spans:
        if sampled >= limit:
            break
        if span.provider == "stub":
            skipped_stub += 1
            continue
        if not (span.output_preview or "").strip():
            skipped_no_preview += 1
            continue
        source = _span_source(span.id)
        exists = db.scalar(select(MemoryShard.id).where(MemoryShard.source == source))
        if exists:
            skipped_already += 1
            continue
        shard = mem_svc.add_memory(
            db,
            text_body=_sample_text(span),
            scope="global",
            source=source,
            project_id=span.project_id or "core",
            status="candidate",
            origin=SAMPLE_ORIGIN,
        )
        sampled += 1
        created_ids.append(shard.id)
    return {
        "sampled": sampled,
        "skipped_stub": skipped_stub,
        "skipped_no_preview": skipped_no_preview,
        "skipped_already": skipped_already,
        "ids": created_ids,
    }


def labels(db: Session, *, project_id: str | None = None) -> dict:
    """How far the human-eval queue has been labelled.

    `absent` — no eval-sample shards exist (nobody sampled, or nothing was eligible).
    `ungraded` — candidates remain; an unlabelled sample is not a pass.
    `ok` — every sampled shard is published or rejected.
    """
    from sqlalchemy import func, select

    from app.models import MemoryShard

    stmt = select(MemoryShard.status, func.count()).where(
        MemoryShard.origin == SAMPLE_ORIGIN,
    )
    if project_id:
        stmt = stmt.where(MemoryShard.project_id == project_id)
    stmt = stmt.group_by(MemoryShard.status)
    counts = {status: n for status, n in db.execute(stmt)}
    candidates = int(counts.get("candidate") or 0)
    published = int(counts.get("published") or 0)
    rejected = int(counts.get("rejected") or 0)
    total = candidates + published + rejected
    if total == 0:
        status = "absent"
        reason = "no eval samples — the human-eval queue has not been looked at"
    elif candidates:
        status = "ungraded"
        reason = (
            f"{candidates} sample(s) still in Memory review; unlabelled is not a pass"
        )
    else:
        status = "ok"
        reason = ""
    return {
        "status": status,
        "graded": status == "ok",
        "reason": reason,
        "candidates": candidates,
        "published": published,
        "rejected": rejected,
        "total": total,
    }


def promote(db: Session, shard_id: str) -> dict:
    """JSON a human can paste into `app/evals/cases/`. Does not write the repo.

    Refuses candidates: freezing an unlabelled sample would mint a golden case
    nobody judged.
    """
    from app.models import MemoryShard

    shard = db.get(MemoryShard, shard_id)
    if shard is None:
        raise ValueError(f"shard not found: {shard_id}")
    if (shard.origin or "") != SAMPLE_ORIGIN:
        raise ValueError(
            f"{shard_id} is not an eval sample (origin {shard.origin!r})"
        )
    if shard.status != "published":
        raise UnlabelledSample(
            f"{shard_id} is {shard.status}: publish it in Memory review before "
            "promoting, or the golden set would freeze an unlabelled sample"
        )
    feature = ""
    for line in (shard.text or "").splitlines():
        if line.startswith("Eval sample · "):
            parts = line.split(" · ")
            if len(parts) >= 2:
                feature = parts[1].strip()
            break
    preview = (shard.text or "").split("\n\n", 1)[-1].strip()
    needle = preview[:80].split("\n")[0].strip()
    return {
        "id": f"from-{shard.source or shard.id}",
        "surface": feature or "extract_lessons",
        "source": shard.source,
        "note": "promoted from a labelled eval sample; fill `input` from the original work",
        "expect": {
            "min_shards": 1,
            **({"must_contain": [needle]} if needle else {}),
        },
        "preview": preview,
    }
