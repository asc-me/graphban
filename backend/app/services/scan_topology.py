"""Scan board cells from the posted app topology (GRPH-1124 / PRD-P58 §Cells).

The overlay lists boxes from the latest `AppTopology` record only — not
`COMPONENT_PATHS`, not `own_deployables`, and not code-graph nodes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.services import code_graph as code_svc

# Lockfiles and Python manifests named in the map become security cells at that path.
_SECURITY_BASENAMES = frozenset({
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
})
_PYTHON_MANIFESTS = frozenset({
    "requirements.txt",
    "pyproject.toml",
    "Pipfile",
})


@dataclass(frozen=True)
class ScanCell:
    """One box on the scan board."""

    id: str
    path: str
    #: Categories this box carries (subset of scans.CATEGORIES).
    categories: frozenset[str]
    #: When True, prefix matching must not treat every path as inside this cell.
    exact_path: bool


def _cell_path_from_dir(directory: str) -> tuple[str, bool]:
    """`(path, is_file_cell)` from a topology component `dir`."""
    d = (directory or "").strip().replace("\\", "/")
    while d.startswith("./"):
        d = d[2:]
    d = d.strip("/")
    if d == ".":
        d = ""
    if not d:
        return "", False
    base = d.rsplit("/", 1)[-1]
    if base in _SECURITY_BASENAMES or base in _PYTHON_MANIFESTS:
        return d, True
    return d + "/", False


def _is_source_dir(directory: str, *, is_file_cell: bool) -> bool:
    if is_file_cell:
        return False
    # Every directory component is a source tree for stub/deadcode/refactor/tests/work.
    return True


def _under(prefix: str, path: str) -> bool:
    pref = prefix.rstrip("/")
    p = (path or "").strip().removeprefix("./")
    if not pref:
        return p == pref
    return p == pref or p.startswith(pref + "/")


def mapping_for_scans(db: Session, project_id: str) -> dict[str, Any]:
    """Same honesty as GET /api/platform/stack `mapping`, for the health overlay."""
    from app.services import stack as stack_svc

    record, unreadable = stack_svc._load_app_record(db, project_id)
    return stack_svc._mapping_block(record, unreadable)


def scan_cells(db: Session, project_id: str) -> tuple[dict[str, Any], list[ScanCell]]:
    """`(mapping, cells)`. Empty cells only when unmapped or the record cannot be read."""
    mapping = mapping_for_scans(db, project_id)
    if mapping.get("state") == "unmapped":
        return mapping, []

    from app.services import stack as stack_svc

    record, unreadable = stack_svc._load_app_record(db, project_id)
    if unreadable:
        return mapping, []
    if record is None:
        return mapping, []

    components = list(record.components or [])
    cells: list[ScanCell] = []
    seen_paths: set[str] = set()

    for comp in components:
        if not isinstance(comp, dict):
            continue
        raw_id = str(comp.get("id") or "").strip()
        directory = comp.get("dir")
        if directory is None:
            continue
        path, is_file = _cell_path_from_dir(str(directory))
        if not path and not is_file:
            # Repo-root directory component — source cell uses the dir label, not "".
            path = ""
        if is_file:
            if path in seen_paths:
                continue
            seen_paths.add(path)
            cid = raw_id or path
            cells.append(ScanCell(
                id=f"sec:{cid}",
                path=path,
                categories=frozenset({"security"}),
                exact_path=True,
            ))
            continue
        if not _is_source_dir(str(directory), is_file_cell=False):
            continue
        # Directory cell: reject empty path (root deployable uses lockfile paths for security).
        if not path:
            continue
        if path in seen_paths:
            continue
        seen_paths.add(path)
        cid = raw_id or path.rstrip("/")
        cells.append(ScanCell(
            id=cid,
            path=path,
            categories=frozenset({"refactor", "stub", "deadcode", "tests", "work"}),
            exact_path=False,
        ))

    # Security-only rows: lockfile paths may also appear without a matching dir cell.
    for comp in components:
        if not isinstance(comp, dict):
            continue
        prov = comp.get("provenance") if isinstance(comp.get("provenance"), dict) else {}
        file_ = str(prov.get("file") or "").strip().replace("\\", "/")
        if not file_:
            continue
        base = file_.rsplit("/", 1)[-1]
        if base not in _SECURITY_BASENAMES and base not in _PYTHON_MANIFESTS:
            continue
        if file_ in seen_paths:
            continue
        seen_paths.add(file_)
        raw_id = str(comp.get("id") or "").strip() or file_
        cells.append(ScanCell(
            id=f"sec:{raw_id}",
            path=file_,
            categories=frozenset({"security"}),
            exact_path=True,
        ))

    total_nodes = sum(1 for _ in code_svc.list_nodes(db, project_id))
    if total_nodes == 0:
        cells.append(ScanCell(
            id="_coverage:not_computable",
            path="__coverage__",
            categories=frozenset({"coverage"}),
            exact_path=True,
        ))
    else:
        for comp in components:
            if not isinstance(comp, dict):
                continue
            directory = comp.get("dir")
            if directory is None:
                continue
            path, is_file = _cell_path_from_dir(str(directory))
            if is_file or not path:
                continue
            raw_id = str(comp.get("id") or "").strip()
            has_nodes = any(
                _under(path, n.path) for n in code_svc.list_nodes(db, project_id)
            )
            if not has_nodes:
                continue
            cov_id = f"cov:{raw_id}" if raw_id else f"cov:{path.rstrip('/')}"
            if any(c.id == cov_id for c in cells):
                continue
            cells.append(ScanCell(
                id=cov_id,
                path=path,
                categories=frozenset({"coverage"}),
                exact_path=False,
            ))

    return mapping, cells
