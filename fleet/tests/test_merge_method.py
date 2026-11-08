"""`gitops.merge_method` picks the `gh pr merge` flag (GRPH-1428).

Sabotage: hard-code `--squash` in `_merge` -> `test_merge_method_merge_*` fails.
"""
from __future__ import annotations

from gbfleet import propose as propose_mod

from tests.test_propose import REVIEWED, REPO, _Forge, gh  # noqa: F401
from tests.test_until_rebase_merge import _Behind


def _run(forge, gh_wire, **kw):
    gh_wire(forge)
    return propose_mod.merge(REPO, "SA-1", "gb/w-1", reviewed=REVIEWED, green={REVIEWED}, **kw)


def _merge_argv(forge):
    return next(a for a in forge.calls if " ".join(a[1:3]) == "pr merge")


def test_unset_merge_method_squashes_as_before(gh):
    forge = _Forge(draft=False)
    got = _run(forge, gh)
    assert got.ok
    assert "--squash" in _merge_argv(forge)


def test_merge_method_merge_passes_merge_flag(gh):
    forge = _Forge(draft=False)
    got = _run(forge, gh, method="merge")
    assert got.ok
    argv = _merge_argv(forge)
    assert "--merge" in argv and "--squash" not in argv


def test_merge_method_rebase_passes_rebase_flag(gh):
    forge = _Forge(draft=False)
    _run(forge, gh, method="rebase")
    argv = _merge_argv(forge)
    assert "--rebase" in argv and "--squash" not in argv


def test_merge_method_merge_updates_a_behind_branch_without_rebase(gh):
    forge = _Behind()
    got = _run(forge, gh, method="merge")
    assert got.pending
    assert "--rebase" not in forge.calls[-1]


def test_squash_updates_a_behind_branch_with_rebase(gh):
    forge = _Behind()
    _run(forge, gh, method="squash")
    assert "--rebase" in forge.calls[-1]


def test_roster_reader_defaults_to_squash():
    assert propose_mod.merge_method(None) == "squash"
    assert propose_mod.merge_method({"merge_method": {"method": "bogus"}}) == "squash"
    assert propose_mod.merge_method({"merge_method": {"method": "merge"}}) == "merge"
