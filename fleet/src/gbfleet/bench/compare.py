"""Side-by-side comparison of two baseline runs (GRPH-P98 section 7). Pure: no I/O."""

from __future__ import annotations


class CompareRefused(Exception):
    """The two runs are not comparable."""


def find_run(results: list[dict], ref: str) -> dict:
    """The baseline record whose `run_id` or `id` is `ref`."""
    for r in results:
        if ref in (r.get("run_id"), r.get("id")):
            return r
    raise CompareRefused(f"no baseline run {ref!r}")


def _label(r: dict) -> str:
    return f"{r.get('vendor')}:{r.get('model')}@{r.get('binary_version') or '-'}"


def compare(a: dict, b: dict) -> list[str]:
    """Lines comparing `a` with `b`, per task and per capability.

    Refuses when the suite id or suite version differ, naming both.
    """
    if (a.get("suite_id"), a.get("suite_version")) != (b.get("suite_id"), b.get("suite_version")):
        raise CompareRefused(
            f"suites differ: {a.get('suite_id')} v{a.get('suite_version')} vs "
            f"{b.get('suite_id')} v{b.get('suite_version')}; runs are comparable only on the "
            "same suite id and version")
    out = [f"suite {a['suite_id']} v{a['suite_version']}", f"A {_label(a)}", f"B {_label(b)}",
           "", "per task"]
    ta = {o["task_id"]: o["outcome"] for o in a.get("outcomes") or []}
    tb = {o["task_id"]: o["outcome"] for o in b.get("outcomes") or []}
    for tid in sorted(set(ta) | set(tb)):
        x, y = ta.get(tid, "-"), tb.get(tid, "-")
        out.append(f"  {tid}: {x} | {y}" + ("" if x == y else "  *"))
    out += ["", "per capability"]
    ra, rb = a.get("rollups") or {}, b.get("rollups") or {}

    def fmt(r: dict | None) -> str:
        return f"{r['green']}/{r['n']}" if r else "-"
    for cap in sorted(set(ra) | set(rb)):
        out.append(f"  {cap}: {fmt(ra.get(cap))} | {fmt(rb.get(cap))}")
    return out
