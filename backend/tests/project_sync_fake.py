"""In-process console for the project-sync contract (GRPH-P89 S0).

Other P89 items drive ``app.services.project_sync_client`` against
``FakeConsole.transport()``. The fake is the contract, not a stub that answers
200 to everything: it stores entities, refuses a single entity without failing
the batch, injects 429 and timeouts, answers 422 for an unknown contract major,
403 ``not_home``, 200 ``adopted`` after a revoked home, and serves the proposal
queue with an ETag and a cursor.

Unlink is not a client call. ``revoke`` is the console noticing a revoked
deployment: it closes that deployment's open proposals as ``refused: box unlinked``.
"""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from app.services.project_sync_client import (
    CONTRACT, ENTITY_TYPES, MAX_ENTITY_BYTES, OUTCOME_STATUSES,
)

_PUBLIC = ("id", "kind", "target", "base_version", "payload", "proposer", "created_at")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _canonical(entity: dict) -> bytes | None:
    try:
        return json.dumps(entity, separators=(",", ":"), sort_keys=True).encode()
    except (TypeError, ValueError):
        return None


@dataclass
class _Deployment:
    token: str
    engineer: str
    label: str
    revoked: bool = False


@dataclass
class _Replica:
    cloud_project_id: str
    tag: str
    name: str
    home_token: str
    engineer: str
    owner_label: str
    entities: dict = field(default_factory=dict)
    revision: int = 1


class FakeConsole:
    """One console. Register every deployment the box will present before calling."""

    def __init__(self, *, max_entity_bytes: int = MAX_ENTITY_BYTES):
        self.max_entity_bytes = max_entity_bytes
        self.requests: list[dict] = []
        self.last_status: int | None = None
        self._deployments: dict[str, _Deployment] = {}
        self._by_tag: dict[str, _Replica] = {}
        self._by_id: dict[str, _Replica] = {}
        self._proposals: dict[str, dict] = {}
        self._outcomes: dict[str, dict] = {}
        self._ids = 0
        self._proposal_seq = 0
        self._raise: BaseException | None = None
        self._status: int | None = None
        self._retry_after: int | None = None

    def add_deployment(self, token: str, *, engineer: str, label: str,
                       revoked: bool = False) -> None:
        """A deployment the console will recognise. ``engineer`` is what adopt
        compares; ``label`` is the ``owner_label`` a ``tag_taken`` names."""
        self._deployments[token] = _Deployment(token, engineer, label, revoked)

    def revoke(self, token: str) -> None:
        """The deployment's credential is revoked. Open proposals on replicas it
        still homes close as ``refused: box unlinked``. A later ``register_project``
        from the same engineer adopts the replica; this method is not a wire call."""
        dep = self._deployments.get(token)
        if dep is None:
            raise KeyError(token)
        dep.revoked = True
        for replica in self._by_id.values():
            if replica.home_token != token:
                continue
            self._close_open(replica, reason="box unlinked")

    def enqueue_proposal(self, cloud_project_id: str, *, kind: str,
                         target: dict | None, base_version: int | None, payload: dict,
                         proposer: dict) -> dict:
        """A cloud member's proposal, waiting for the box to pull it."""
        replica = self._require_replica(cloud_project_id)
        self._proposal_seq += 1
        replica.revision += 1
        proposal = {
            "id": f"pr{self._proposal_seq}",
            "kind": kind,
            "target": target,
            "base_version": base_version,
            "payload": dict(payload),
            "proposer": dict(proposer),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "_seq": self._proposal_seq,
            "_open": True,
            "_project": cloud_project_id,
        }
        self._proposals[proposal["id"]] = proposal
        return self._public(proposal)

    def entities(self, cloud_project_id: str) -> list[dict]:
        replica = self._require_replica(cloud_project_id)
        return list(replica.entities.values())

    def outcome_for(self, proposal_id: str) -> dict | None:
        stored = self._outcomes.get(proposal_id)
        return dict(stored) if stored is not None else None

    def timeout_once(self) -> None:
        """The next call raises ``httpx.ReadTimeout`` before any status."""
        self._raise = httpx.ReadTimeout("timed out")

    def fail_once(self, exc: BaseException) -> None:
        self._raise = exc

    def status_once(self, code: int) -> None:
        """The next call answers this status with an empty JSON object."""
        self._status = code

    def retry_after_once(self, seconds: int) -> None:
        """The next call answers 429 with ``Retry-After: seconds``."""
        self._retry_after = seconds

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({
            "method": request.method,
            "path": request.url.path,
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "params": dict(request.url.params),
        })
        if self._raise is not None:
            exc, self._raise = self._raise, None
            raise exc
        if self._status is not None:
            code, self._status = self._status, None
            return self._respond(code, json={})
        if self._retry_after is not None:
            seconds, self._retry_after = self._retry_after, None
            return self._respond(429, headers={"Retry-After": str(seconds)})
        token, missing = self._token(request)
        if missing is not None:
            return missing
        refused = self._contract_header(request)
        if refused is not None:
            return refused
        path = request.url.path
        if path == "/api/sync/contract" and request.method == "GET":
            return self._respond(200, json={"version": CONTRACT})
        if path == "/api/sync/projects" and request.method == "POST":
            return self._register(request, token)
        if path == "/api/sync/project-entities" and request.method == "POST":
            return self._push(request, token)
        if path == "/api/sync/project-entities" and request.method == "DELETE":
            return self._purge(request, token)
        if path == "/api/sync/proposals" and request.method == "GET":
            return self._pull(request, token)
        if path == "/api/sync/proposals/outcomes" and request.method == "POST":
            return self._outcomes_call(request, token)
        return self._problem(404, "not_found")

    # ---- routing ------------------------------------------------------------------------

    def _token(self, request: httpx.Request) -> tuple[str, httpx.Response | None]:
        auth = request.headers.get("authorization", "")
        if auth[:7].lower() == "bearer ":
            token = auth[7:].strip()
        else:
            token = request.headers.get("x-api-key", "").strip()
        if not token:
            return "", self._problem(401, "unauthorized")
        dep = self._deployments.get(token)
        if dep is None:
            return "", self._problem(401, "unauthorized")
        if dep.revoked:
            return "", self._problem(401, "token_revoked")
        return token, None

    def _contract_header(self, request: httpx.Request) -> httpx.Response | None:
        major = request.headers.get("x-sync-contract")
        pushes = request.method in ("POST", "DELETE")
        if major is None:
            if pushes:
                return self._problem(422, "contract", field="X-Sync-Contract")
            return None
        if major != CONTRACT:
            return self._problem(422, "contract", field="X-Sync-Contract")
        return None

    def _register(self, request: httpx.Request, token: str) -> httpx.Response:
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            return self._problem(422, "invalid", field="body")
        if not isinstance(body, dict):
            return self._problem(422, "invalid", field="body")
        tag, name = body.get("tag"), body.get("name")
        if not isinstance(tag, str) or not tag:
            return self._problem(422, "invalid", field="tag")
        if not isinstance(name, str):
            return self._problem(422, "invalid", field="name")
        dep = self._deployments[token]
        existing = self._by_tag.get(tag)
        if existing is None:
            self._ids += 1
            replica = _Replica(
                cloud_project_id=f"cp{self._ids}", tag=tag, name=name, home_token=token,
                engineer=dep.engineer, owner_label=dep.label,
            )
            self._by_tag[tag] = self._by_id[replica.cloud_project_id] = replica
            return self._respond(201, json={"cloud_project_id": replica.cloud_project_id, "tag": tag})
        home = self._deployments.get(existing.home_token)
        home_revoked = home is None or home.revoked
        if home_revoked and dep.engineer == existing.engineer:
            # Same replica. A fresh relink re-attaches; it does not mint a new id.
            existing.home_token = token
            existing.owner_label = dep.label
            existing.name = name
            return self._respond(200, json={
                "cloud_project_id": existing.cloud_project_id, "tag": tag, "adopted": True,
            })
        return self._problem(409, "tag_taken", owner_label=existing.owner_label)

    def _push(self, request: httpx.Request, token: str) -> httpx.Response:
        if request.headers.get("content-encoding") != "gzip":
            return self._problem(422, "invalid", field="body")
        try:
            body = json.loads(gzip.decompress(request.content))
        except (OSError, EOFError, ValueError):
            return self._problem(422, "invalid", field="body")
        if not isinstance(body, dict):
            return self._problem(422, "invalid", field="body")
        if str(body.get("contract")) != CONTRACT:
            return self._problem(422, "contract", field="contract")
        project = request.headers.get("x-cloud-project", "")
        replica = self._home(project, token)
        if isinstance(replica, httpx.Response):
            return replica
        entities = body.get("entities")
        if not isinstance(entities, list):
            return self._problem(422, "invalid", field="entities")
        stored, refused = [], []
        for entity in entities:
            row, reason = self._one_entity(replica, entity)
            if reason is None:
                stored.append(row)
            else:
                refused.append({**row, "reason": reason})
        return self._respond(200, json={"stored": stored, "refused": refused})

    def _one_entity(self, replica: _Replica, entity: object) -> tuple[dict, str | None]:
        if not isinstance(entity, dict):
            return {"type": "", "id": "", "version": 0}, "invalid"
        identity = {
            "type": entity.get("type") if isinstance(entity.get("type"), str) else "",
            "id": entity.get("id") if isinstance(entity.get("id"), str) else "",
            "version": entity.get("version") if _is_int(entity.get("version")) else 0,
        }
        encoded = _canonical(entity)
        if encoded is None:
            return identity, "invalid"
        # Canonical JSON, not the gzip batch. An entity that cannot fit one D11
        # batch is too_large on its own; the rest of the batch still stores.
        if len(encoded) > self.max_entity_bytes:
            return identity, "too_large"
        if identity["type"] not in ENTITY_TYPES:
            return identity, "forbidden_type"
        digest = entity.get("hash")
        if (not identity["id"] or not _is_int(entity.get("version"))
                or not isinstance(digest, str) or not digest.startswith("sha256:")
                or digest == "sha256:" or not isinstance(entity.get("body"), dict)):
            return identity, "invalid"
        key = (identity["type"], identity["id"], identity["version"])
        if key not in replica.entities:
            # First write wins. A replay of the same version is a stored no-op,
            # not a conflict and not a second row.
            replica.entities[key] = dict(entity)
        return identity, None

    def _purge(self, request: httpx.Request, token: str) -> httpx.Response:
        project = request.url.params.get("project", "")
        replica = self._home(project, token)
        if isinstance(replica, httpx.Response):
            return replica
        deleted = len(replica.entities)
        replica.entities.clear()
        self._close_open(replica, reason="project unsynced")
        return self._respond(200, json={"deleted": deleted})

    def _pull(self, request: httpx.Request, token: str) -> httpx.Response:
        project = request.url.params.get("project", "")
        replica = self._home(project, token)
        if isinstance(replica, httpx.Response):
            return replica
        etag = f'"{replica.revision}"'
        if request.headers.get("if-none-match") == etag:
            return self._respond(304, headers={"ETag": etag})
        after_raw = request.url.params.get("after", "")
        after = int(after_raw) if after_raw.isdigit() else 0
        seqs = [p["_seq"] for p in self._proposals.values() if p["_project"] == project]
        cursor = str(max(seqs) if seqs else 0)
        page = [self._public(p) for p in sorted(self._proposals.values(), key=lambda p: p["_seq"])
                if p["_project"] == project and p["_open"] and p["_seq"] > after]
        return self._respond(200, json={"cursor": cursor, "proposals": page},
                             headers={"ETag": etag})

    def _outcomes_call(self, request: httpx.Request, token: str) -> httpx.Response:
        del token  # outcomes are addressed by proposal id; the credential was already checked
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            return self._problem(422, "invalid", field="body")
        if not isinstance(body, dict) or not isinstance(body.get("outcomes"), list):
            return self._problem(422, "invalid", field="outcomes")
        incoming = body["outcomes"]
        parsed: list[dict] = []
        for row in incoming:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                return self._problem(422, "invalid", field="id")
            status = row.get("status")
            if status not in OUTCOME_STATUSES:
                return self._problem(422, "invalid", field="status")
            parsed.append(row)
        for row in parsed:
            prev = self._outcomes.get(row["id"])
            if prev is not None and prev["status"] != row["status"]:
                # The whole request writes nothing. A mixed batch must not land
                # the outcomes that were fine and refuse one.
                return self._problem(409, "already_resolved")
        for row in parsed:
            if row["id"] in self._outcomes:
                continue
            record = {"status": row["status"]}
            if isinstance(row.get("reason"), str):
                record["reason"] = row["reason"]
            if _is_int(row.get("current_version")):
                record["current_version"] = row["current_version"]
            self._outcomes[row["id"]] = record
            proposal = self._proposals.get(row["id"])
            if proposal is not None and proposal["_open"]:
                proposal["_open"] = False
                replica = self._by_id.get(proposal["_project"])
                if replica is not None:
                    replica.revision += 1
        return self._respond(200, json={})

    def _home(self, project: str, token: str) -> _Replica | httpx.Response:
        replica = self._by_id.get(project)
        if replica is None or replica.home_token != token:
            return self._problem(403, "not_home")
        return replica

    def _close_open(self, replica: _Replica, *, reason: str) -> None:
        closed = False
        for proposal in self._proposals.values():
            if proposal["_project"] != replica.cloud_project_id or not proposal["_open"]:
                continue
            proposal["_open"] = False
            self._outcomes[proposal["id"]] = {"status": "refused", "reason": reason}
            closed = True
        if closed:
            replica.revision += 1

    def _require_replica(self, cloud_project_id: str) -> _Replica:
        replica = self._by_id.get(cloud_project_id)
        if replica is None:
            raise KeyError(cloud_project_id)
        return replica

    def _public(self, proposal: dict) -> dict:
        return {key: proposal[key] for key in _PUBLIC}

    def _respond(self, status: int, *, json: dict | None = None,
                 headers: dict | None = None) -> httpx.Response:
        self.last_status = status
        if json is None:
            return httpx.Response(status, headers=headers)
        return httpx.Response(status, json=json, headers=headers)

    def _problem(self, status: int, detail: str, **extra: str) -> httpx.Response:
        return self._respond(status, json={"detail": detail, **extra})
