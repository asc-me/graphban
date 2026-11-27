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

#: The console's pull row (graphban-control docs/contract.md, Proposals): flat, and
#: ``target_type`` / ``target_id`` / ``base_version`` are omitted when null, as the
#: console's ``toMember`` omits them. GRPH-1842: this fake once served a nested
#: ``target`` / ``proposer``, so the box read a shape the real console never sends.
_PUBLIC = ("id", "kind", "target_type", "target_id", "base_version", "payload",
           "proposer_label", "created_at")
_OMIT_WHEN_NULL = ("target_type", "target_id", "base_version")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


#: ``Number.MAX_SAFE_INTEGER``. The console's ``classifyEntity`` requires
#: ``Number.isSafeInteger(version) && version >= 0``, so ``2^53`` is already invalid.
MAX_SAFE_VERSION = 2**53 - 1


def _safe_version(value: object) -> bool:
    return _is_int(value) and 0 <= value <= MAX_SAFE_VERSION


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
    #: GBC-P23. Non-home deployments that can reach this project: token -> True.
    members: dict = field(default_factory=dict)
    #: The console's ``sync`` choice per deployment token (``full``/``summaries``/None).
    choice: dict = field(default_factory=dict)
    #: Tokens whose ``GET /api/sync/projects`` entry leaves ``home`` out (an older console).
    home_omitted: set = field(default_factory=set)
    #: The effective level each deployment last reported. Absent is "never reported".
    effective: dict = field(default_factory=dict)
    #: A non-home deployment's own ``item`` rows: token -> {(type, id): entity}.
    box_entities: dict = field(default_factory=dict)


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
        #: GBC-P23. What the box-push receipt lists in ``capabilities``.
        self.receipt_capabilities: list[str] = []
        #: What ``GET /api/config`` advertises.
        self.features: dict = {"item_digests": True}
        #: Every ``POST /api/sync/project-sync`` the console stored: (token, project, level).
        self.effective_reports: list[tuple[str, str, str]] = []
        #: Every 403/422 answered, as (path, status, detail, field).
        self.refusals: list[tuple[str, int, str, str | None]] = []
        #: Accepted bodies: digests by (token, project name), people by (token, project name).
        self.digests: dict = {}
        self.people: dict = {}
        #: Every body the console accepted (digest rows and entities), for leak checks.
        self.accepted_bodies: list = []

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
        """A cloud member's proposal, waiting for the box to pull it.

        ``target`` is ``{type, id}`` and ``proposer`` is ``{label, ...}`` for the
        test's convenience. On the wire they become the console's flat
        ``target_type`` / ``target_id`` / ``proposer_label``.
        """
        replica = self._require_replica(cloud_project_id)
        self._proposal_seq += 1
        replica.revision += 1
        target = target if isinstance(target, dict) else {}
        proposal = {
            "id": f"pr{self._proposal_seq}",
            "kind": kind,
            "target_type": target.get("type"),
            "target_id": target.get("id"),
            "base_version": base_version,
            "payload": dict(payload),
            "proposer_label": str(proposer.get("label") or ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "_seq": self._proposal_seq,
            "_open": True,
            "_project": cloud_project_id,
        }
        self._proposals[proposal["id"]] = proposal
        return self._public(proposal)

    # ---- GBC-P23 setup (not wire calls) -------------------------------------------------

    def set_choice(self, cloud_project_id: str, token: str, sync: str | None, *,
                   omit_home: bool = False) -> None:
        """The console's ``sync`` choice for this deployment on this project."""
        replica = self._require_replica(cloud_project_id)
        replica.choice[token] = sync
        if omit_home:
            replica.home_omitted.add(token)
        else:
            replica.home_omitted.discard(token)

    def share(self, cloud_project_id: str, token: str, *, sync: str | None = None) -> None:
        """``token`` can reach the project without being its home (a team member's box)."""
        replica = self._require_replica(cloud_project_id)
        replica.members[token] = True
        replica.choice[token] = sync

    def box_entities(self, cloud_project_id: str, token: str) -> list[dict]:
        replica = self._require_replica(cloud_project_id)
        return list(replica.box_entities.get(token, {}).values())

    def install(self, monkeypatch) -> "FakeConsole":
        """Route the box's module-level ``httpx`` calls (digests, people, box push, the
        projects read) and every ``httpx.Client`` through this console."""
        real_client = httpx.Client
        transport = self.transport()

        def _client(*args, **kwargs):
            if kwargs.get("transport") is None:
                kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        def _call(method):
            def call(url, **kwargs):
                kwargs.pop("timeout", None)
                with real_client(transport=transport) as c:
                    return c.request(method, url, **kwargs)
            return call

        def _request(method, url, **kwargs):
            return _call(method)(url, **kwargs)

        monkeypatch.setattr(httpx, "Client", _client)
        monkeypatch.setattr(httpx, "get", _call("GET"))
        monkeypatch.setattr(httpx, "post", _call("POST"))
        monkeypatch.setattr(httpx, "request", _request)
        return self

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
        path = request.url.path
        if path == "/api/config" and request.method == "GET":
            return self._respond(200, json={"features": dict(self.features)})
        token, missing = self._token(request)
        if missing is not None:
            return missing
        # The legacy-key routes the box posts without X-Sync-Contract.
        if path == "/api/sync/item-digests" and request.method == "POST":
            return self._digests(request, token)
        if path == "/api/sync/item-digests" and request.method == "DELETE":
            return self._respond(200, json={"deleted": True})
        if path == "/api/sync/assignable-people" and request.method == "POST":
            return self._people_call(request, token)
        if path == "/api/sync/box-push" and request.method == "POST":
            return self._respond(200, json={"capabilities": list(self.receipt_capabilities)})
        if path == "/api/sync/projects" and request.method == "GET":
            return self._projects(token)
        refused = self._contract_header(request)
        if refused is not None:
            return refused
        if path == "/api/sync/contract" and request.method == "GET":
            return self._respond(200, json={"version": CONTRACT})
        if path == "/api/sync/projects" and request.method == "POST":
            return self._register(request, token)
        if path == "/api/sync/project-sync" and request.method == "POST":
            return self._project_sync(request, token)
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
        replica = self._visible(project, token)
        if isinstance(replica, httpx.Response):
            return replica
        entities = body.get("entities")
        if not isinstance(entities, list):
            return self._problem(422, "invalid", field="entities")
        is_home = replica.home_token == token
        full = replica.effective.get(token) == "full"
        if not is_home:
            # GBC-P23 per-box items: a non-home box owns only `item` records.
            for entity in entities:
                if isinstance(entity, dict) and entity.get("type") in ("prd", "comment", "wait"):
                    return self._problem(403, "not_home")
            if not full:
                return self._problem(
                    422, "a non-home item push needs effective_sync full", field="entities")
        # Holders: exact keys, and only while the stored level for this box is `full`.
        # Checked over the whole request first: a refused request stores nothing.
        for entity in entities:
            if isinstance(entity, dict) and entity.get("type") == "item" and isinstance(
                    entity.get("body"), dict):
                problem = self._holder_problem(entity["body"], full)
                if problem is not None:
                    return problem
        # Subclasses override `_one_entity(replica, entity)`, so the per-box target rides
        # on the instance for the length of this request.
        self._target = replica.entities if is_home else replica.box_entities.setdefault(token, {})
        stored, refused = [], []
        for entity in entities:
            row, reason = self._one_entity(replica, entity)
            if reason is None:
                stored.append(row)
            else:
                refused.append({**row, "reason": reason})
        return self._respond(200, json={"stored": stored, "refused": refused})

    def _one_entity(self, replica: _Replica, entity: object) -> tuple[dict, str | None]:
        target = getattr(self, "_target", None)
        target = replica.entities if target is None else target
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
        if (not identity["id"] or not _safe_version(entity.get("version"))
                or not isinstance(digest, str) or not digest.startswith("sha256:")
                or digest == "sha256:" or not isinstance(entity.get("body"), dict)):
            return identity, "invalid"
        # One row per entity. A greater version replaces the body. An older or
        # equal version is acknowledged and changes nothing (GBC-P19): the
        # response still says stored, and the replica keeps the body it has.
        key = (identity["type"], identity["id"])
        current = target.get(key)
        if current is None or identity["version"] > current.get("version", -1):
            target[key] = dict(entity)
            self.accepted_bodies.append(dict(entity))
        return identity, None

    def _purge(self, request: httpx.Request, token: str) -> httpx.Response:
        project = request.url.params.get("project", "")
        replica = self._visible(project, token)
        if isinstance(replica, httpx.Response):
            return replica
        if replica.home_token != token:
            # GBC-P23: DELETE removes only the caller's own rows.
            own = replica.box_entities.pop(token, {})
            return self._respond(200, json={"deleted": len(own)})
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
        # The console names the project on outcomes, as on every project-scoped
        # call: ``?project=`` or ``X-Cloud-Project``, else 422; home only, else 403.
        project = (request.url.params.get("project", "").strip()
                   or request.headers.get("x-cloud-project", "").strip())
        if not project:
            return self._problem(422, "project is required", field="project")
        replica = self._home(project, token)
        if isinstance(replica, httpx.Response):
            return replica
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

    # ---- GBC-P23 routes ------------------------------------------------------------------

    def _projects(self, token: str) -> httpx.Response:
        rows = []
        for replica in self._by_id.values():
            is_home = replica.home_token == token
            if not is_home and token not in replica.members:
                continue
            row = {"id": replica.cloud_project_id, "tag": replica.tag, "name": replica.name,
                   "sync": replica.choice.get(token)}
            if token not in replica.home_omitted:
                row["home"] = is_home
            rows.append(row)
        return self._respond(200, json={"projects": rows})

    def _project_sync(self, request: httpx.Request, token: str) -> httpx.Response:
        project = request.headers.get("x-cloud-project", "")
        replica = self._visible(project, token)
        if isinstance(replica, httpx.Response):
            return replica
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            return self._problem(422, "invalid", field="body")
        level = body.get("effective_sync") if isinstance(body, dict) else None
        if level not in ("full", "summaries"):
            return self._problem(422, "effective_sync must be full or summaries",
                                 field="effective_sync")
        replica.effective[token] = level
        self.effective_reports.append((token, project, level))
        return self._respond(200, json={"project_id": project, "effective_sync": level})

    def _by_name(self, name: str, token: str) -> _Replica | None:
        for replica in self._by_id.values():
            if replica.name == name and (replica.home_token == token or token in replica.members):
                return replica
        return None

    def _digests(self, request: httpx.Request, token: str) -> httpx.Response:
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            return self._problem(422, "invalid", field="body")
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            return self._problem(422, "invalid", field="items")
        name = str(body.get("project") or "")
        replica = self._by_name(name, token)
        full = replica is not None and replica.effective.get(token) == "full"
        for row in body["items"]:
            if not isinstance(row, dict):
                return self._problem(422, "invalid", field="items")
            problem = self._holder_problem(row, full)
            if problem is not None:
                return problem
        self.digests[(token, name)] = list(body["items"])
        self.accepted_bodies.extend(body["items"])
        return self._respond(200, json={"record": "item_digests", "count": len(body["items"])})

    def _people_call(self, request: httpx.Request, token: str) -> httpx.Response:
        name = request.url.params.get("project", "")
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            return self._problem(422, "invalid", field="body")
        if not isinstance(body, dict) or set(body) != {"people"} or not isinstance(
                body.get("people"), list):
            return self._problem(422, "people must be a list", field="people")
        allowed = {"id", "name"}
        if "assignable_self" in self.receipt_capabilities:
            allowed = {"id", "name", "self"}
        marked = 0
        for person in body["people"]:
            if not isinstance(person, dict):
                return self._problem(422, "each person is {id, name}", field="people")
            bad = [k for k in person if k not in allowed]
            if bad:
                return self._problem(422, f"{bad[0]} is not a person field", field=bad[0])
            if not all(isinstance(person.get(k), str) and person[k].strip()
                       for k in ("id", "name")):
                return self._problem(422, "each person needs id and name", field="people")
            if "self" in person:
                if person["self"] is not True:
                    return self._problem(422, "self must be true", field="self")
                marked += 1
        if marked > 1:
            return self._problem(422, "at most one person is self", field="self")
        self.people[(token, name)] = list(body["people"])
        return self._respond(200, json={"record": "assignable_people",
                                        "count": len(body["people"])})

    def _holder_problem(self, row: dict, full: bool) -> httpx.Response | None:
        """#339 "Holders on the wire": exact keys, and only under a stored ``full``."""
        present = [k for k in ("assignee", "held_by") if k in row]
        if not present:
            return None
        if not full:
            return self._problem(422, "holder fields need effective_sync full",
                                 field=present[0])
        assignee = row.get("assignee")
        if "assignee" in row and assignee is not None:
            if not isinstance(assignee, dict) or set(assignee) != {"id", "name"}:
                return self._problem(422, "assignee is {id, name}", field="assignee")
        held = row.get("held_by")
        if "held_by" in row and held is not None:
            if not isinstance(held, dict) or not {"kind", "label"} <= set(held) or (
                    set(held) - {"kind", "label", "owner", "agent_kind"}):
                return self._problem(422, "held_by is {kind, label, owner?, agent_kind?}",
                                     field="held_by")
            if held["kind"] not in ("agent", "person"):
                return self._problem(422, "held_by.kind is agent or person", field="held_by")
            owner = held.get("owner")
            if owner is not None and (not isinstance(owner, dict) or set(owner) != {"id", "name"}):
                return self._problem(422, "held_by.owner is {id, name}", field="held_by")
        return None

    def _visible(self, project: str, token: str) -> _Replica | httpx.Response:
        replica = self._by_id.get(project)
        if replica is None or (replica.home_token != token and token not in replica.members):
            return self._problem(403, "not_home")
        return replica

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
        return {key: proposal[key] for key in _PUBLIC
                if not (key in _OMIT_WHEN_NULL and proposal[key] is None)}

    def _respond(self, status: int, *, json: dict | None = None,
                 headers: dict | None = None) -> httpx.Response:
        self.last_status = status
        if json is None:
            return httpx.Response(status, headers=headers)
        return httpx.Response(status, json=json, headers=headers)

    def _problem(self, status: int, detail: str, **extra: str) -> httpx.Response:
        if status in (403, 422):
            path = self.requests[-1]["path"] if self.requests else ""
            self.refusals.append((path, status, detail, extra.get("field")))
        return self._respond(status, json={"detail": detail, **extra})
