"""Box → console project-sync client (GRPH-P89 S0).

One function per contract call. Every call returns ``ok``, ``refused`` (the console
said no, and ``reason`` is its ``detail``), or ``unknown`` (no answer: the link is
missing, the network failed, the call timed out, or the console answered 408, 425,
429, or 5xx). ``unknown`` is never a refusal. A 429 carries ``retry_after`` seconds
from ``Retry-After``.

The credential is the deployment token (or the migrated key) from
``code_sync._console_auth`` — the same headers ``code_sync`` already sends, including
``X-Cloud-Project`` on a project-scoped call. The token is not a result field and is
not written to the log. Unlink and device-sign-in relink are not calls here; see
``docs/project-sync.md``.
"""
from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import NotRequired, TypedDict

import httpx
from sqlalchemy.orm import Session

from app.services.code_sync import NotLinked, _console_auth

log = logging.getLogger(__name__)

CONTRACT = "1"
DEFAULT_MAX_DELAY = 60  # P89 D11: the box's max flush delay, sent as X-Sync-Max-Delay
# One entity that cannot fit a D11 backfill batch (1 MiB, whichever of count or
# bytes comes first) is refused ``too_large``. The console measures canonical JSON,
# not the gzip wire size — gzip would hide the entity that cannot fit.
MAX_ENTITY_BYTES = 1024 * 1024
TIMEOUT = 30.0

ENTITY_TYPES = ("item", "prd", "comment", "wait")
PROPOSAL_KINDS = ("comment", "answer", "edit", "create")
OUTCOME_STATUSES = ("applied", "refused", "conflict")
ENTITY_REFUSAL_REASONS = ("too_large", "invalid", "forbidden_type")


class Entity(TypedDict):
    type: str
    id: str
    version: int
    hash: str
    body: dict


class Outcome(TypedDict):
    id: str
    status: str
    reason: NotRequired[str]
    current_version: NotRequired[int]


@dataclass
class SyncResult:
    """One contract call. ``status`` is ``ok``, ``refused``, or ``unknown``.

    A push that stored some entities and refused others is still ``ok``: the
    per-entity refusals are ``refused``, and the call itself succeeded. ``refused``
    on the call means the console refused the whole request.
    """

    status: str
    reason: str | None = None
    retry_after: int | None = None
    field: str | None = None
    owner_label: str | None = None
    version: str | None = None
    cloud_project_id: str | None = None
    tag: str | None = None
    adopted: bool | None = None
    stored: list[dict] = dc_field(default_factory=list)
    refused: list[dict] = dc_field(default_factory=list)
    deleted: int | None = None
    cursor: str | None = None
    proposals: list[dict] = dc_field(default_factory=list)
    etag: str | None = None
    not_modified: bool = False


def get_contract(db: Session, *, transport: httpx.BaseTransport | None = None) -> SyncResult:
    """``GET /api/sync/contract`` → ``{version: "1"}``."""
    result, resp = _call(db, "GET", "/api/sync/contract", transport=transport)
    if result is not None:
        return result
    data = _object(resp)
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not version:
        return SyncResult(status="unknown", reason="the console answered without contract version 1")
    # A different version is a refusal that still names what the console speaks.
    # Dropping it made a contract change indistinguishable from a network failure,
    # and the flusher could not park against the version it was shown.
    if version != CONTRACT:
        return SyncResult(status="refused", reason="contract", version=version, field="version")
    return SyncResult(status="ok", version=version)


def register_project(db: Session, *, tag: str, name: str,
                     transport: httpx.BaseTransport | None = None) -> SyncResult:
    """``POST /api/sync/projects`` ``{tag, name}``.

    ``201`` is a new replica. ``200`` with ``adopted`` re-attaches to the tag's
    existing replica after its home deployment was revoked for the same engineer.
    ``409`` ``tag_taken`` names ``owner_label``.
    """
    result, resp = _call(db, "POST", "/api/sync/projects",
                         json_body={"tag": tag, "name": name}, transport=transport)
    if result is not None:
        return result
    data = _object(resp)
    if data is None:
        return SyncResult(status="unknown", reason="the console answered without a project")
    cloud_id, answered_tag = data.get("cloud_project_id"), data.get("tag")
    if not isinstance(cloud_id, str) or not cloud_id or not isinstance(answered_tag, str):
        return SyncResult(status="unknown", reason="the console answered without a project")
    adopted = data.get("adopted")
    if isinstance(adopted, bool):
        flag = adopted
    else:
        # 200 is the adopt answer. A 201 that omits the field did not adopt.
        flag = resp.status_code == 200
    return SyncResult(status="ok", cloud_project_id=cloud_id, tag=answered_tag, adopted=flag)


def push_entities(db: Session, *, cloud_project_id: str, entities: list[Entity],
                  max_delay: int = DEFAULT_MAX_DELAY,
                  transport: httpx.BaseTransport | None = None) -> SyncResult:
    """``POST /api/sync/project-entities``. Gzip body, ``X-Cloud-Project``, and
    ``X-Sync-Max-Delay`` (the box's max flush delay; D11 default 60).

    ``ok`` carries ``stored`` and the per-entity ``refused`` list (``too_large``,
    ``invalid``, ``forbidden_type``). The call is ``ok`` even when some entities
    were refused — the flusher parks those and counts the rest as stored.
    """
    try:
        raw = json.dumps({"contract": CONTRACT, "entities": list(entities)},
                         separators=(",", ":"), sort_keys=True).encode()
    except (TypeError, ValueError):
        return SyncResult(status="unknown", reason="unreadable_entity")
    result, resp = _call(
        db, "POST", "/api/sync/project-entities", content=gzip.compress(raw),
        extra_headers={
            "X-Cloud-Project": cloud_project_id,
            "X-Sync-Max-Delay": str(int(max_delay)),
            "Content-Encoding": "gzip",
            "Content-Type": "application/json",
        },
        transport=transport,
    )
    if result is not None:
        return result
    data = _object(resp)
    parsed = _entity_lists(data) if data is not None else None
    if parsed is None:
        return SyncResult(status="unknown", reason="the console answered without stored and refused")
    stored, refused = parsed
    return SyncResult(status="ok", stored=stored, refused=refused)


def purge_entities(db: Session, *, project: str,
                   transport: httpx.BaseTransport | None = None) -> SyncResult:
    """``DELETE /api/sync/project-entities?project=`` → ``{deleted}``.

    ``project`` is the cloud project id. The console closes open proposals as
    ``refused: project unsynced``; that close is console state, not a field here.
    """
    result, resp = _call(db, "DELETE", "/api/sync/project-entities",
                         params={"project": project},
                         extra_headers={"X-Cloud-Project": project}, transport=transport)
    if result is not None:
        return result
    data = _object(resp)
    deleted = data.get("deleted") if isinstance(data, dict) else None
    if not _is_int(deleted):
        return SyncResult(status="unknown", reason="the console answered without a deleted count")
    return SyncResult(status="ok", deleted=deleted)


def pull_proposals(db: Session, *, project: str, after: str = "", etag: str | None = None,
                   transport: httpx.BaseTransport | None = None) -> SyncResult:
    """``GET /api/sync/proposals?project=&after=``.

    ``304`` is ``ok`` with ``not_modified`` (an idle poll, which is what keeps the
    console from marking the project stale). The caller keeps its previous cursor:
    a 304 body has none. A ``200`` returns ``cursor``, ``proposals``, and ``etag``.
    """
    result, resp = _call(db, "GET", "/api/sync/proposals",
                         params={"project": project, "after": after},
                         extra_headers={"X-Cloud-Project": project},
                         etag=etag, transport=transport)
    if result is not None:
        return result
    data = _object(resp)
    proposals = _proposals(data) if data is not None else None
    cursor = data.get("cursor") if isinstance(data, dict) else None
    if proposals is None or not isinstance(cursor, str):
        return SyncResult(status="unknown", reason="the console answered without a proposal page")
    return SyncResult(status="ok", cursor=cursor, proposals=proposals,
                      etag=resp.headers.get("etag"), not_modified=False)


def report_outcomes(db: Session, *, project: str, outcomes: list[Outcome],
                    transport: httpx.BaseTransport | None = None) -> SyncResult:
    """``POST /api/sync/proposals/outcomes?project=``. The same outcome again is ``ok``.
    A different status for an id the console already resolved is ``refused``
    with ``already_resolved``.

    ``project`` is the cloud project id. The console answers ``422 project is
    required`` without it (GRPH-1842), so it goes as ``?project=`` and as
    ``X-Cloud-Project``, the same as ``pull_proposals``. One call names one project.
    """
    result, resp = _call(db, "POST", "/api/sync/proposals/outcomes",
                         json_body={"outcomes": list(outcomes)},
                         params={"project": project},
                         extra_headers={"X-Cloud-Project": project}, transport=transport)
    if result is not None:
        return result
    if _object(resp) is None:
        return SyncResult(status="unknown", reason="the console answered without an outcome result")
    return SyncResult(status="ok")


# ---- wire -------------------------------------------------------------------------------

def _call(db: Session, method: str, path: str, *, json_body: dict | None = None,
          content: bytes | None = None, params: dict | None = None,
          extra_headers: dict | None = None, etag: str | None = None,
          transport: httpx.BaseTransport | None = None,
          ) -> tuple[SyncResult | None, httpx.Response | None]:
    """Send one call. ``(result, None)`` is finished; ``(None, response)`` is a 2xx
    the caller still has to read. Network failures stay in this function so a
    timeout cannot be reported as ``refused`` by a caller that only sees a result.
    """
    try:
        base, auth = _console_auth(db)
    except NotLinked:
        _logged(method, path, "unknown")
        return SyncResult(status="unknown", reason="not_linked"), None
    headers = {**auth, "X-Sync-Contract": CONTRACT, **(extra_headers or {})}
    if etag:
        headers["If-None-Match"] = etag
    url = f"{base.rstrip('/')}{path}"
    try:
        with httpx.Client(transport=transport, timeout=TIMEOUT, follow_redirects=False) as client:
            resp = client.request(method, url, headers=headers, params=params,
                                  json=json_body, content=content)
    except (httpx.HTTPError, OSError) as e:
        # Class name only. str(e) can carry the request, and the request carries
        # the Authorization header. A timeout mapped to refused fails
        # test_a_timeout_is_unknown_and_not_refused.
        _logged(method, path, "unknown")
        return SyncResult(status="unknown",
                          reason=f"could not reach the console: {type(e).__name__}"), None
    finished = _terminal(resp)
    if finished is not None:
        _logged(method, path, "ok" if finished.not_modified else finished.status)
        return finished, None
    _logged(method, path, "ok")
    return None, resp


def _terminal(resp: httpx.Response) -> SyncResult | None:
    """Map everything that is not a 2xx body. ``None`` means the caller reads the body.

    304 is ``ok``: an idle poll must not look like a failure or the console marks
    the project stale. 429 is ``unknown`` plus ``retry_after``, not a no.
    """
    code = resp.status_code
    if code == 304:
        return SyncResult(status="ok", not_modified=True, etag=resp.headers.get("etag"))
    if 200 <= code < 300:
        return None
    if code == 429:
        raw = (resp.headers.get("retry-after") or "").strip()
        seconds = int(raw) if raw.isdigit() else None
        return SyncResult(status="unknown", reason="rate_limited", retry_after=seconds)
    if 400 <= code < 500 and code not in (408, 425):
        reason, field_name, owner, version = _refusal(resp, code)
        return SyncResult(status="refused", reason=reason, field=field_name,
                          owner_label=owner, version=version)
    return SyncResult(status="unknown", reason=f"the console answered {code}")


def _refusal(resp: httpx.Response, code: int) -> tuple[str, str | None, str | None, str | None]:
    data = _object(resp)
    if not isinstance(data, dict):
        return f"http_{code}", None, None, None
    reason = None
    for key in ("detail", "reason", "error"):
        found = data.get(key)
        if isinstance(found, str) and found:
            reason = found
            break
    field_name = data.get("field") if isinstance(data.get("field"), str) else None
    owner = data.get("owner_label") if isinstance(data.get("owner_label"), str) else None
    version = data.get("version") if isinstance(data.get("version"), str) and data.get("version") else None
    return reason or f"http_{code}", field_name, owner, version


def _object(resp: httpx.Response) -> dict | None:
    if not resp.content:
        return {}
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _logged(method: str, path: str, status: str) -> None:
    # Path and status only. The credential is a header and must not be interpolated.
    log.info("project sync %s %s -> %s", method, path, status)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _entity_row(row: object, *, with_reason: bool) -> dict | None:
    """The four contract fields. Anything else the console echoed — a body, a
    header — is dropped, so a result cannot carry the credential back."""
    if not isinstance(row, dict):
        return None
    out = {
        "type": row.get("type") if isinstance(row.get("type"), str) else "",
        "id": row.get("id") if isinstance(row.get("id"), str) else "",
        "version": row.get("version") if _is_int(row.get("version")) else 0,
    }
    if with_reason:
        reason = row.get("reason")
        out["reason"] = reason if isinstance(reason, str) else ""
    return out


def _entity_lists(data: dict | None) -> tuple[list[dict], list[dict]] | None:
    if not isinstance(data, dict):
        return None
    stored, refused = data.get("stored"), data.get("refused")
    if not isinstance(stored, list) or not isinstance(refused, list):
        return None
    parsed_stored, parsed_refused = [], []
    for row in stored:
        item = _entity_row(row, with_reason=False)
        if item is None:
            return None
        parsed_stored.append(item)
    for row in refused:
        item = _entity_row(row, with_reason=True)
        if item is None:
            return None
        parsed_refused.append(item)
    return parsed_stored, parsed_refused


#: The console's pull row (graphban-control docs/contract.md, Proposals). Flat:
#: the target is ``target_type`` + ``target_id`` and the proposer is
#: ``proposer_label``. A key the console omitted reads as ``None``.
_PROPOSAL_KEYS = ("id", "kind", "target_type", "target_id", "base_version", "payload",
                  "proposer_label", "created_at")


def _proposals(data: dict | None) -> list[dict] | None:
    if not isinstance(data, dict) or not isinstance(data.get("proposals"), list):
        return None
    out = []
    for row in data["proposals"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            return None
        out.append({key: row.get(key) for key in _PROPOSAL_KEYS})
    return out
