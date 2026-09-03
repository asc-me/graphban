"""Platform + integration config (Phase 5).

The AI-provider settings genuinely drive F1: updating llm_mode switches the live
chat/extraction provider (Ollama/Anthropic/stub). The embed provider stays a
deploy-time setting because changing it changes the pgvector column dimension.

GitHub/Drive here manage connection *state and config* — live OAuth/token exchange
and API sync are intentionally out of scope for the local slice (no third-party
credentials); the inbound GitHub webhook (routers/public.py) is fully implemented.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from secrets import token_urlsafe

from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app import providers
from app.services import failover
from app.providers import probe
from app.providers import registry as provider_registry
from app.providers.base import ChatModel, Extractor
from app.models import Credential, DeploymentConfig, PlatformConfig, Project
from app.security import secrets


logger = logging.getLogger("graphban.platform")


def get_config(db: Session, project_id: str = "core") -> PlatformConfig:
    cfg = db.get(PlatformConfig, project_id)
    if cfg is None:
        cfg = PlatformConfig(project_id=project_id)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def apply_llm(cfg: PlatformConfig) -> None:
    """Point the live provider layer at the configured chat provider, and bridge Ollama's
    endpoint/model into the (deploy-time) embedder."""
    if cfg.active_chat_provider:
        # New provider-registry path.
        pconf = (cfg.providers or {}).get(cfg.active_chat_provider, {})
        app_settings.chat_provider = cfg.active_chat_provider  # keep the legacy mirror in sync
        providers.set_active_chat(
            provider=cfg.active_chat_provider,
            base_url=pconf.get("base_url", ""),
            api_key=secrets.decrypt(pconf.get("api_key", "")),
            model=pconf.get("chat_model", ""),
        )
    elif cfg.llm_mode == "local":
        # Legacy llm_mode path (kept for back-compat; sets app_settings.chat_provider too).
        app_settings.chat_provider = "ollama"
        app_settings.ollama_base_url = cfg.local_base_url
        app_settings.ollama_chat_model = cfg.local_model
        providers.set_active_chat("ollama", base_url=cfg.local_base_url, model=cfg.local_model)
    elif cfg.llm_mode == "cloud":
        app_settings.chat_provider = "anthropic"
        app_settings.anthropic_model = cfg.cloud_model
        providers.set_active_chat("anthropic", model=cfg.cloud_model)
    else:
        app_settings.chat_provider = "stub"
        providers.set_active_chat("stub")

    # A UI-configured Ollama serves embeddings too when EMBED_PROVIDER=ollama (deploy-time):
    # push its endpoint/model/auth into the env-selected embedder.
    ollama_conf = (cfg.providers or {}).get("ollama", {})
    if ollama_conf.get("base_url"):
        app_settings.ollama_base_url = ollama_conf["base_url"]
    if ollama_conf.get("embed_model"):
        app_settings.ollama_embed_model = ollama_conf["embed_model"]
    if ollama_conf.get("api_key"):
        app_settings.ollama_auth_key = secrets.decrypt(ollama_conf["api_key"])
    providers.reset()


def _chat_params(cfg: PlatformConfig) -> tuple[str, str, str, str]:
    """(provider, base_url, api_key, model) for a project's own saved config. This is the
    per-project resolver — the live chat/extraction provider is derived here at call time,
    NOT from the process-global providers._active, which can only hold one project at once."""
    if cfg.active_chat_provider:
        pconf = (cfg.providers or {}).get(cfg.active_chat_provider, {})
        return (
            cfg.active_chat_provider,
            pconf.get("base_url", ""),
            secrets.decrypt(pconf.get("api_key", "")),
            pconf.get("chat_model", ""),
        )
    if cfg.llm_mode == "local":
        return ("ollama", cfg.local_base_url, "", cfg.local_model)
    if cfg.llm_mode == "cloud":
        return ("anthropic", "", "", cfg.cloud_model)
    return ("stub", "", "", "")


@dataclass(frozen=True)
class Resolved:
    """What a project's chat resolution actually landed on, and by which route (PRD-25 D-g).

    Returning a bare `(provider_id, model)` pair could not answer the one question §4 needs
    asked: a provider id of `"stub"` means both *this deployment has no provider configured*
    and *this project points at a credential that no longer exists*, and those want opposite
    responses from an operator. `source` separates them.
    """

    provider_id: str
    chat: ChatModel
    model: str = ""
    credential_id: str = ""
    #: `project` the project's credential pointer · `deployment` the scope default · `stub`
    #: nothing is configured · `dangling` a pointer was set and did not resolve, and nothing
    #: downstream caught it.
    #:
    #: `legacy` is GONE (S6). It marked a resolution from the per-project blob, and existed so
    #: "how much is still unmigrated" was answerable by the running system. Once the migration
    #: has run there is nothing left for it to describe.
    source: str = "stub"
    #: The credential this project ASKED for and did not get, when resolution fell past it
    #: (GRPH-525). Empty on every ordinary resolution.
    #:
    #: **This is the warning, and it is a field rather than only a log line for a reason.**
    #: §4 settled that a failing project credential falls back rather than stopping — and that
    #: the objection to falling back was never the fallback, it was the SILENCE. A caller that
    #: cannot tell it was substituted has no way to say so, and `source` does not carry it:
    #: `source="deployment"` looks identical whether the project asked for that credential or
    #: was quietly moved onto it.
    fell_back_from: str = ""

    @property
    def substituted(self) -> bool:
        """Whether this project got something other than what it pointed at."""
        return bool(self.fell_back_from)


def scope_for(db: Session, project_id: str) -> str:
    """The `deployment_config.scope` a project reads its default from.

    `""` is the deployment itself — the self-hosted posture, where `projects.org_id` is NULL
    for everything. Under hosted multi-tenancy it is the project's org, so one org's default
    can never be handed to another org's project.
    """
    project = db.get(Project, project_id)
    return (project.org_id or "") if project is not None else ""


def credential_in_scope(db: Session, credential_id: str | None, scope: str) -> Credential | None:
    """A credential, but only if it belongs to the scope asking for it.

    The scope check is not decoration. Without it a stale or hand-edited pointer would reach
    across tenants, and the pointer is exactly the thing that outlives the row it names.
    """
    if not credential_id:
        return None
    cred = db.get(Credential, credential_id)
    if cred is None or (cred.org_id or "") != scope:
        return None
    return cred


def usable(cred: "Credential | None") -> bool:
    """Whether a PROJECT pointer at this credential can be honoured (GRPH-525).

    Shared by `resolve_chat` and `list_credentials` deliberately. The console has to show the
    same set of projects that resolution actually falls past, and two implementations of "is
    this usable" is how the view and the behaviour drift into disagreeing — the console saying
    a project is fine while its calls go somewhere else.

    `unreachable` is not usable: it was asked and did not answer. `pending_validation` IS —
    nobody has asked yet, and treating unproven as broken would drop a project onto the
    default the moment it was configured, before a single probe ran.

    This is about a PROJECT pointer. The deployment default is deliberately returned even when
    unreachable: there is nothing below it but the stub, so routing around it would hide a
    broken default forever.
    """
    return cred is not None and cred.state != UNREACHABLE


def _fallback_for(db: Session, scope: str, primary_id: str):
    """The scope's fallback credential, if it is usable and is not the one that just resolved.

    Three refusals, each with its own reason:

    - **not set** — failover is opt-in; absent means errors surface unchanged (S3).
    - **the same credential** — falling over to the credential that just failed spends a
      second call to be told the same thing.
    - **not usable** — an `unreachable` fallback is SKIPPED rather than probed on the request
      path. A synchronous probe there would put an unbounded network call inside a
      user-facing request to rescue a configuration the operator already broke.
    """
    row = db.get(DeploymentConfig, scope or "")
    fallback_id = row.fallback_credential_id if row else None
    if not fallback_id or fallback_id == primary_id:
        return None
    cred = credential_in_scope(db, fallback_id, scope)
    return cred if usable(cred) else None


def _from_credential(cred: Credential, source: str, model_override: str = "",
                     *, fell_back_from: str = "", db: Session | None = None,
                     scope: str = "", project_id: str = "") -> Resolved:
    """Build the adapter from a credential row, honouring a project's model override.

    The override reaches `build_chat` and not just the returned `model` field. Reporting an
    override that the adapter did not receive would be worse than not having the feature: the
    UI would show the cheap model, the bill would show the expensive one, and nothing in
    between would disagree.
    """
    model = model_override or cred.model
    # project_id binds span attribution (GRPH-225) at construction — the resolution
    # that answered "which provider for THIS project" is the authority on that field.
    chat = providers.build_chat(
        cred.kind, base_url=cred.base_url,
        api_key=secrets.decrypt(cred.api_key), model=model, project_id=project_id,
    )

    # Wrapped only when a usable fallback exists, so a deployment without one gets exactly the
    # object it got before this slice — and its provider errors surface unchanged.
    second = _fallback_for(db, scope, cred.id) if db is not None else None
    if second is not None:
        chat = failover.FailoverChat(
            primary=chat, primary_id=cred.id,
            fallback=providers.build_chat(
                second.kind, base_url=second.base_url,
                api_key=secrets.decrypt(second.api_key), model=second.model,
                project_id=project_id,
            ),
            fallback_id=second.id,
        )

    return Resolved(
        provider_id=cred.kind,
        chat=chat,
        model=model,
        credential_id=cred.id,
        source=source,
        fell_back_from=fell_back_from,
    )


# Named tasks that may point at their own credential (GRPH-316). Unset = inherit
# the project's chat pointer. A dedicated judge model is this set, not a new env
# var — evals.py already deferred here.
CHAT_ROLES = (
    "grill.converse",
    "grill.classify",
    "memory.judge",
    "assistant",
    "spec.critique",
)


def resolve_chat(db: Session, project_id: str) -> Resolved:
    """Which chat provider a project gets, in the transitional order of PRD-25 S1.

    ```
    0. the project's legacy `providers` blob     ← S1-S5 ONLY, removed by S6
    1. the project's `credential_id`             ← nothing sets this yet
    2. the scope's default credential            ← nothing sets this yet
    3. the stub
    ```

    **Step 0 is what makes "S1 changes no behaviour" true rather than hoped.** Every project
    that has configured its own provider today is holding it in that blob, and if the new
    pointers were consulted first they would all be unset — so every one of those projects
    would silently drop to a deployment default nobody has configured, which is the stub. A
    slice advertised as additive would have turned off everyone's LLM.

    Steps 1 and 2 read storage that no product code writes yet. They are implemented here, and
    tested, because a branch that ships unexercised is a branch nobody has checked: S2 adds the
    surface that sets these pointers, and finds the ordering already proven rather than
    discovering it against live data.
    """
    # STEP 0 IS GONE (PRD-25 S6). The project's legacy `providers` blob used to be consulted
    # first, so that S1-S5 could ship without moving anyone's configuration. GRPH-512 migrated
    # every entry into a credential row, and the step died with the data it read: a resolution
    # branch that outlives its data is a branch nothing exercises.
    #
    # The blob itself is still on disk, deliberately. The grill amended "delete it in the same
    # transaction" to "leave it as a read-only vestige", because deleting the only copy of the
    # old configuration in the same breath as writing the new one is what makes a bad migration
    # unrecoverable. Resolution never reads it; the boot migration still does — guarded by
    # `credential_migrated` so a pointer an operator cleared is not re-pointed from here.
    scope = scope_for(db, project_id)
    project = db.get(Project, project_id)
    pointer = getattr(project, "credential_id", None) if project is not None else None

    cred = credential_in_scope(db, pointer, scope)
    if usable(cred):
        return _from_credential(cred, "project",
                                getattr(project, "model_override", "") or "",
                                db=db, scope=scope, project_id=project_id)

    # The project asked for something it is not getting. `fell_back_from` carries that the
    # whole way down, so every outcome below reports the substitution rather than looking
    # like an ordinary resolution (GRPH-525).
    wanted = pointer or ""
    if wanted:
        why = ("is unreachable" if cred is not None
               else "does not resolve in this scope")
        logger.warning(
            "project %s asked for credential %s, which %s; falling back",
            project_id, wanted, why,
        )

    row = db.get(DeploymentConfig, scope)
    default = credential_in_scope(db, row.default_credential_id if row else None, scope)
    if default is not None:
        # **An unreachable DEFAULT is still returned** — deliberately asymmetric with the
        # project credential above. There is nothing below the default but the stub, so
        # routing around it would hide a broken default forever; a project credential has
        # somewhere to fall to, so it falls.
        return _from_credential(default, "deployment", fell_back_from=wanted,
                                db=db, scope=scope, project_id=project_id)

    # Nothing resolved. `dangling` when a pointer was SET and did not survive — the operator
    # has something to fix, and it is not the same situation as never having configured one.
    return Resolved(provider_id="stub",
                    chat=providers.build_chat("stub", project_id=project_id),
                    source="dangling" if pointer else "stub",
                    fell_back_from=wanted)


def resolve_role(db: Session, project_id: str, role: str) -> Resolved:
    """Chat resolution for one named task (GRPH-316).

    Unset inherits `resolve_chat` exactly — anyone who does not care keeps one
    setting. A role that NAMES a credential and cannot use it is `role_unusable`
    (stub), not a silent fall back to the project's model: grading with a weaker
    bar is the grill's recorded failure, and it applies here too.
    """
    if role not in CHAT_ROLES:
        raise ValueError(f"unknown chat role {role!r}; known: {', '.join(CHAT_ROLES)}")
    project = db.get(Project, project_id)
    spec = ((project.chat_roles or {}).get(role) if project is not None else None) or {}
    if not isinstance(spec, dict):
        spec = {}
    cred_id = (spec.get("credential_id") or "") or None
    model_over = spec.get("model_override") or ""
    if not cred_id and not model_over:
        return resolve_chat(db, project_id)

    scope = scope_for(db, project_id)
    if cred_id:
        cred = credential_in_scope(db, cred_id, scope)
        if usable(cred):
            model = model_over or (project.model_override if project is not None else "")
            return _from_credential(cred, "role", model,
                                    db=db, scope=scope, project_id=project_id)
        return Resolved(
            provider_id="stub",
            chat=providers.build_chat("stub", project_id=project_id),
            source="role_unusable",
            fell_back_from=cred_id,
        )

    # Model-only override on the inherited credential.
    inherited = resolve_chat(db, project_id)
    if inherited.credential_id:
        cred = credential_in_scope(db, inherited.credential_id, scope)
        if usable(cred):
            return _from_credential(cred, inherited.source, model_over,
                                    fell_back_from=inherited.fell_back_from,
                                    db=db, scope=scope, project_id=project_id)
    return inherited


def set_project_roles(db: Session, project_id: str, roles: dict) -> Project:
    """Replace the project's per-task map. Unknown roles and foreign credentials refuse.

    Sending `{}` clears every override (inherit). Sending `{role: {}}` or `{role: null}`
    clears that one role. A credential_id must be in the project's scope.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise LookupError(project_id)
    if not isinstance(roles, dict):
        raise ValueError("chat_roles must be an object")
    cleaned: dict = {}
    scope = scope_for(db, project_id)
    for name, spec in roles.items():
        if name not in CHAT_ROLES:
            raise ValueError(f"unknown chat role {name!r}; known: {', '.join(CHAT_ROLES)}")
        if spec is None or spec == {}:
            continue
        if not isinstance(spec, dict):
            raise ValueError(f"chat role {name!r} must be an object")
        cred_id = spec.get("credential_id") or None
        model_over = spec.get("model_override") or ""
        if cred_id:
            cred = credential_in_scope(db, cred_id, scope)
            if cred is None:
                raise LookupError(cred_id)
        if not cred_id and not model_over:
            continue
        entry: dict = {}
        if cred_id:
            entry["credential_id"] = cred_id
        if model_over:
            entry["model_override"] = str(model_over)
        cleaned[name] = entry
    project.chat_roles = cleaned
    db.commit()
    db.refresh(project)
    return project


def chat_model_for(db: Session, project_id: str) -> ChatModel:
    return resolve_chat(db, project_id).chat


def extractor_for(db: Session, project_id: str) -> Extractor:
    provider, base_url, api_key, model = _chat_params(get_config(db, project_id))
    return providers.build_extractor(provider, base_url=base_url, api_key=api_key,
                                     model=model, project_id=project_id)


# ---- Per-conversation model picker (AL-176) --------------------------------------------
# The assistant lets each thread pick its OWN provider (chat with Claude on one, Grok on
# another) independent of the project's single active chat provider — extends the
# per-project resolution above to an arbitrary configured provider.
def _provider_params_for(cfg: PlatformConfig, provider_id: str) -> tuple[str, str, str, str]:
    pconf = (cfg.providers or {}).get(provider_id, {})
    return (provider_id, pconf.get("base_url", ""), secrets.decrypt(pconf.get("api_key", "")),
            pconf.get("chat_model", ""))


def resolve_chat_for(db: Session, project_id: str, provider_id: str) -> tuple[str, ChatModel]:
    """(provider_id, chat model) for a SPECIFIC provider a thread chose — not the project's
    active one. An unconfigured / stub pick resolves to the offline stub."""
    provider, base_url, api_key, model = _provider_params_for(get_config(db, project_id), provider_id)
    return provider, providers.build_chat(provider, base_url=base_url, api_key=api_key,
                                          model=model, project_id=project_id)


def _is_configured(cfg: PlatformConfig, prov: dict) -> bool:
    """A provider is selectable for the assistant only if it's actually usable: the offline
    stub can't drive tool-calling; Ollama needs an endpoint; the rest need a key."""
    pconf = (cfg.providers or {}).get(prov["id"], {})
    if prov["kind"] == "stub":
        return False
    if prov["kind"] == "ollama":
        return bool(pconf.get("base_url") or prov.get("base_url"))
    return bool(pconf.get("api_key"))


def selectable_providers(db: Session, project_id: str) -> list[dict]:
    """The provider catalog for the assistant's model picker: each real provider with
    whether it's `configured` (selectable) and whether it's the project's `active` one.
    The UI disables unconfigured picks with a pointer to Settings."""
    cfg = get_config(db, project_id)
    out = []
    for prov in provider_registry.PROVIDERS:
        if prov["kind"] == "stub":
            continue  # the offline stub is never an assistant model
        chat_model = (cfg.providers or {}).get(prov["id"], {}).get("chat_model") or prov["chat_model"]
        # the models the picker offers: the registry's list, else just the default; a
        # user-configured custom model always stays selectable (prepended if not listed)
        models = list(prov.get("models") or ([prov["chat_model"]] if prov["chat_model"] else []))
        if chat_model and chat_model not in models:
            models = [chat_model, *models]
        out.append({
            "id": prov["id"], "label": prov["label"], "kind": prov["kind"],
            "chat_model": chat_model, "models": models,
            "configured": _is_configured(cfg, prov),
            "active": prov["id"] == cfg.active_chat_provider,
        })
    return out


_LLM_FIELDS = {
    "llm_mode", "local_base_url", "local_model", "cloud_provider", "cloud_model",
    "active_chat_provider", "providers",
}


class UnknownModel(ValueError):
    """A model name the provider says it does not have (GRPH-485)."""


def _check_models(provider_id: str, conf: dict, incoming: dict) -> None:
    """Refuse a model name the provider can be asked about and does not have.

    Checked HERE rather than at the call site, because a bad name saves cleanly and then
    fails at every consumer — and the consumers report it badly. The incident: a chat_model
    of `qwen3.6:35b-a3b-coding-mtp` (a tag the host did not have) broke every chat call for
    an hour while the PRD grill it took down reported "your answers are still outstanding".
    One edit from correct, and nothing said so.

    **Only a provider that answered can refuse anything.** `known_models` returns None for a
    provider with no listing endpoint and for one that could not be reached, and both mean
    unchecked. A network blip must not block a correct edit.
    """
    fields = {k: v for k, v in (("chat_model", incoming.get("chat_model")),
                                ("embed_model", incoming.get("embed_model"))) if v}
    if not fields:
        return
    known = probe.known_models(provider_id, conf.get("base_url") or "",
                               secrets.decrypt(conf.get("api_key") or "") or "")
    if not known:  # None (unchecked) or empty (nothing to check against)
        return
    for field, name in fields.items():
        if name not in known:
            close = sorted(k for k in known if name.split(":")[0] in k)[:3]
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            raise UnknownModel(
                f"{provider_id} has no model named {name!r}.{hint} "
                f"It offers {len(known)} models; refusing to save a name that would fail "
                "at every call rather than once, here."
            )


def update_config(db: Session, project_id: str, fields: dict) -> PlatformConfig:
    cfg = get_config(db, project_id)
    touched = set(fields.keys())

    # Providers dict: merge (don't clobber), with write-only key semantics — a blank api_key
    # keeps the stored one, so the redacted round-trip from the UI never wipes a key.
    if fields.get("providers") is not None:
        merged = dict(cfg.providers or {})
        for pid, incoming in (fields["providers"] or {}).items():
            cur = dict(merged.get(pid, {}))
            for k, v in (incoming or {}).items():
                if k == "api_key":
                    # Write-only + encrypted at rest: a blank value keeps the stored
                    # (encrypted) key; a new value is Fernet-encrypted before storage (AL-73).
                    if v:
                        cur["api_key"] = secrets.encrypt(v)
                elif v is not None:
                    cur[k] = v
            _check_models(pid, cur, incoming or {})
            merged[pid] = cur
        cfg.providers = merged  # reassign so SQLAlchemy tracks the JSON change

    for k, v in fields.items():
        if k == "providers":
            continue
        if hasattr(cfg, k) and v is not None:
            setattr(cfg, k, v)

    # Mint a share token the first time public sharing is enabled; keep it stable
    # thereafter so the link survives a disable/re-enable (AL-73).
    if cfg.public_share_enabled and not cfg.share_token:
        cfg.share_token = token_urlsafe(24)

    db.commit()
    db.refresh(cfg)
    if _LLM_FIELDS & touched:
        apply_llm(cfg)
    return cfg


def connect_github(db: Session, project_id: str, *, account: str, repo: str) -> PlatformConfig:
    cfg = get_config(db, project_id)
    cfg.github_connected = True
    cfg.github_account = account
    cfg.github_repo = repo
    cfg.github_scope = "repo · read/write"
    db.commit()
    db.refresh(cfg)
    return cfg


def disconnect_github(db: Session, project_id: str) -> PlatformConfig:
    cfg = get_config(db, project_id)
    cfg.github_connected = False
    cfg.github_account = ""
    cfg.github_repo = ""
    cfg.github_scope = ""
    db.commit()
    db.refresh(cfg)
    return cfg


def connect_gdrive(db: Session, project_id: str, *, account: str, folder: str) -> PlatformConfig:
    cfg = get_config(db, project_id)
    cfg.gdrive_connected = True
    cfg.gdrive_account = account
    cfg.gdrive_folder = folder
    db.commit()
    db.refresh(cfg)
    return cfg


def disconnect_gdrive(db: Session, project_id: str) -> PlatformConfig:
    cfg = get_config(db, project_id)
    cfg.gdrive_connected = False
    cfg.gdrive_account = ""
    cfg.gdrive_folder = ""
    db.commit()
    db.refresh(cfg)
    return cfg


# ---- The deployment credential registry (PRD-25 S1) --------------------------------------


def list_credentials(db: Session, scope: str = "") -> list[dict]:
    """Every credential in one scope, with its state and which projects point at it.

    **`used-by` is derived, never stored.** A stored count is a second copy of a fact the
    pointers already hold, and the two disagree the first time a project is deleted by any path
    that forgets to decrement it — which is how a credential becomes undeletable for a project
    that no longer exists.

    One grouped query regardless of how many projects exist, rather than a lookup per
    credential. At this deployment's size either would be imperceptible; the shape matters
    because S2 refuses to delete a referenced credential and names every referencing project in
    the 409, so this is the query behind an operator-facing error message.

    The api_key is never returned in any form — only `key_set`, exactly as the legacy blob's
    `provider_config` does it.
    """
    creds = (
        db.query(Credential)
        .filter(Credential.org_id == (scope or None))
        .order_by(Credential.created_at, Credential.id)
        .all()
    )
    if not creds:
        return []

    used: dict[str, list[str]] = {}
    rows = (
        db.query(Project.credential_id, Project.id)
        .filter(Project.credential_id.isnot(None))
        .all()
    )
    for credential_id, pid in rows:
        used.setdefault(credential_id, []).append(pid)

    # Which of those pointers are being fallen past, computed from rows ALREADY LOADED.
    #
    # The first version called `resolve_chat` once per pointing project, which was correct and
    # quietly made this function's own docstring false — "one grouped query regardless of how
    # many projects exist" is not true of a function with an N+1 inside it. Sharing `usable`
    # keeps one definition of the rule without paying that.
    in_scope = {c.id: c for c in creds}
    fallen: dict[str, list[str]] = {}
    for credential_id, pids in used.items():
        if not usable(in_scope.get(credential_id)):
            fallen[credential_id] = sorted(pids)

    row = db.get(DeploymentConfig, scope or "")
    default_id = row.default_credential_id if row else None
    fallback_id = row.fallback_credential_id if row else None
    embed_id = row.embed_credential_id if row else None

    return [
        {
            "id": c.id,
            "kind": c.kind,
            "label": c.label,
            "base_url": c.base_url,
            "model": c.model,
            "key_set": c.key_set,
            "state": c.state,
            "last_error": c.last_error,
            "used_by": sorted(used.get(c.id, [])),
            # Projects pointing here that are NOT actually getting it (GRPH-525). §4 says a
            # warning nobody is shown is the same defect as no warning one layer along, and
            # the console is the only surface an operator sees without reading logs. Derived
            # from live resolution rather than stored, for the same reason `used_by` is.
            "falling_back": sorted(fallen.get(c.id, [])),
            "is_default": c.id == default_id,
            "is_fallback": c.id == fallback_id,
            "is_embed": c.id == embed_id,
        }
        for c in creds
    ]


# ---- Choosing a credential, and refusing to strand one (PRD-25 S2) -----------------------

#: A credential that has never been proven to work cannot be made the default or the fallback.
#:
#: **`unreachable` is deliberately NOT in this set**, and the asymmetry is the point (grill,
#: D-f). `pending_validation` means nobody has ever established that this credential works —
#: selecting it would assert something no one has checked. `unreachable` means it WAS asked and
#: did not answer, which is a fact about the world right now, and an operator who points at it
#: anyway has said something. The system's job there is to show the state, not to overrule the
#: choice; at runtime an unreachable fallback is skipped and the primary failure is terminal.
UNPROVEN = "pending_validation"

#: Asked, and did not answer. A PROJECT credential in this state is fallen past (GRPH-525);
#: the deployment default in this state is still returned. See the asymmetry in `resolve_chat`.
UNREACHABLE = "unreachable"


class CredentialInUse(Exception):
    """Deleting would strand a pointer. Carries WHO, because "in use" that does not say by
    what leaves the operator hunting through seven projects by hand."""

    def __init__(self, projects: list[str], roles: list[str]) -> None:
        self.projects = projects
        self.roles = roles
        parts = []
        if projects:
            parts.append("used by " + ", ".join(projects))
        if roles:
            parts.append("set as " + ", ".join(roles))
        super().__init__("; ".join(parts) or "in use")


def _probe_state(kind: str, base_url: str, api_key: str, model: str) -> str:
    """The state a credential should be saved in, from one probe.

    Three outcomes, and only two of them are states — the third is a refusal:

    - the provider answered and has the model  -> `valid`
    - the provider answered and does NOT       -> ValueError, and the caller turns it into the
                                                  422 GRPH-485 established. Unchanged: retry is
                                                  for *could not be asked*, never for *asked and
                                                  told no*.
    - the provider could not be asked          -> `pending_validation`, and S2b retries it

    `known_models` already draws exactly this line and says why — `None` covers both "no listing
    endpoint" and "unreachable", because refusing a save over a briefly-down host would break a
    correct edit for a reason that has nothing to do with the edit.
    """
    known = probe.known_models(kind, base_url or "", api_key or "")
    if known is None:
        return UNPROVEN
    if model and model not in known:
        raise ValueError(
            f"{kind} answered and does not have model {model!r}. It offers: "
            + ", ".join(sorted(known)[:10])
        )
    return "valid"


def create_credential(db: Session, scope: str, *, kind: str, label: str = "",
                      base_url: str = "", api_key: str = "", model: str = "") -> Credential:
    """Add a credential to a scope, probing once to decide its state.

    A row per credential, so adding a second key for a provider that already has one is an
    ordinary insert rather than a collision anybody has to resolve (D-a).
    """
    from secrets import token_urlsafe as _tok

    state = _probe_state(kind, base_url, api_key, model)
    cred = Credential(
        id=f"cred_{_tok(8)}", org_id=scope or None, kind=kind, label=label,
        base_url=base_url, api_key=secrets.encrypt(api_key) if api_key else "",
        model=model, state=state,
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return cred


def update_credential(db: Session, credential_id: str, scope: str, **fields) -> Credential:
    """Edit a credential in place and re-probe.

    **Editing is how rotation happens** — the key, endpoint and model are all editable and the
    row id never changes, so every pointer at this credential keeps pointing at it. Rotation as
    create-and-repoint would mean finding every project that referenced the old row.

    The retry budget resets here because a resave is new information: the thing that could not
    be asked may now be answerable, and a row that stayed `unreachable` after being corrected
    would be reporting the old failure.
    """
    cred = credential_in_scope(db, credential_id, scope)
    if cred is None:
        raise LookupError(credential_id)
    for key in ("kind", "label", "base_url", "model"):
        if key in fields and fields[key] is not None:
            setattr(cred, key, fields[key])
    if fields.get("api_key"):
        cred.api_key = secrets.encrypt(fields["api_key"])
    cred.state = _probe_state(cred.kind, cred.base_url, secrets.decrypt(cred.api_key), cred.model)
    cred.validation_attempts = 0
    cred.next_attempt_at = None
    cred.last_error = ""
    db.commit()
    db.refresh(cred)
    return cred


def _references(db: Session, credential_id: str, scope: str) -> tuple[list[str], list[str]]:
    """Every pointer at this credential: projects that use it, and roles it fills."""
    projects = [
        pid for (pid,) in db.query(Project.id)
        .filter(Project.credential_id == credential_id).all()
    ]
    row = db.get(DeploymentConfig, scope or "")
    roles = []
    if row is not None:
        if row.default_credential_id == credential_id:
            roles.append("the deployment default")
        if row.fallback_credential_id == credential_id:
            roles.append("the fallback")
        if row.embed_credential_id == credential_id:
            roles.append("the embedding credential")
    for p in db.query(Project).all():
        if (p.org_id or "") != (scope or ""):
            continue
        for name, spec in ((p.chat_roles or {}) if isinstance(p.chat_roles, dict) else {}).items():
            if isinstance(spec, dict) and spec.get("credential_id") == credential_id:
                roles.append(f"{p.id} {name}")
    return sorted(projects), roles


def delete_credential(db: Session, credential_id: str, scope: str) -> None:
    """Remove a credential, refusing while anything still points at it.

    The refusal NAMES every referencing project and role. A bare "in use" makes the operator
    open seven projects to find the one holding it, and the `used_by` tags in the listing exist
    precisely so this refusal is predictable before it happens rather than a surprise after.
    """
    cred = credential_in_scope(db, credential_id, scope)
    if cred is None:
        raise LookupError(credential_id)
    projects, roles = _references(db, credential_id, scope)
    if projects or roles:
        raise CredentialInUse(projects, roles)
    db.delete(cred)
    db.commit()


def set_scope_defaults(db: Session, scope: str, *, default_credential_id: str | None = ...,
                       fallback_credential_id: str | None = ...,
                       embed_credential_id: str | None = ...) -> DeploymentConfig:
    """Point a scope's default / fallback / embedding at credentials it owns.

    `...` means "leave alone" and `None` means "clear", which are different intentions and
    would be indistinguishable if absence meant clear.

    Every id is checked for scope AND for proof: a credential nobody has established works
    cannot be made the thing everything falls back to. That check is here rather than only in
    the UI because an unusable credential that can still be chosen is the same defect one layer
    along — the UI merely stops being the place it is noticed.
    """
    row = db.get(DeploymentConfig, scope or "")
    if row is None:
        row = DeploymentConfig(scope=scope or "")
        db.add(row)

    for field, value, what in (
        ("default_credential_id", default_credential_id, "default"),
        ("fallback_credential_id", fallback_credential_id, "fallback"),
    ):
        if value is ...:
            continue
        if value is None:
            setattr(row, field, None)
            continue
        cred = credential_in_scope(db, value, scope)
        if cred is None:
            raise LookupError(value)
        if cred.state == UNPROVEN:
            raise ValueError(
                f"{value} has never been validated, so it cannot be the {what}. Use "
                "Test connection, or correct and resave it, first."
            )
        setattr(row, field, value)

    # The embedder gate is a callee nobody called if this field is setattr'd like the
    # other two. PUT /credentials/defaults used to return 200 while vectors_exist was
    # true; `set_embed_credential` is the probe + dimension + vectors_exist check.
    if embed_credential_id is not ...:
        from app.services import embedder as emb_svc
        if embed_credential_id is None:
            row.embed_credential_id = None
            db.commit()
            db.refresh(row)
            emb_svc.apply_embedder(db, scope)
            return row
        cred = credential_in_scope(db, embed_credential_id, scope)
        if cred is None:
            raise LookupError(embed_credential_id)
        if cred.state == UNPROVEN:
            raise ValueError(
                f"{embed_credential_id} has never been validated, so it cannot be the "
                "embedding credential. Use Test connection, or correct and resave it, first."
            )
        db.commit()
        return emb_svc.set_embed_credential(db, scope, embed_credential_id)

    db.commit()
    db.refresh(row)
    return row


def set_project_credential(db: Session, project_id: str, *,
                           credential_id: str | None = ..., model_override: str | None = ...):
    """Point one project at a credential, optionally on a different model.

    The credential must belong to the project's own scope. Checked on the way in as well as at
    resolution — resolution re-checks because a pointer outlives the row it names, and this
    checks because an error at save is one the operator can act on, where a silent fallback at
    resolution is one they discover from a model answering in the wrong voice.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise LookupError(project_id)
    if credential_id is not ...:
        if credential_id is None:
            project.credential_id = None
        else:
            cred = credential_in_scope(db, credential_id, (project.org_id or ""))
            if cred is None:
                raise LookupError(credential_id)
            project.credential_id = credential_id
        # Any explicit operator decision on this pointer makes the legacy blob vestigial for
        # this project. Without the flag, a pointer set and removed through the console with
        # no boot in between is invisible to the migration's own marking — and the next boot
        # would re-point from the blob, resurrecting a rule the operator just removed.
        project.credential_migrated = True
    if model_override is not ...:
        project.model_override = model_override or ""
    db.commit()
    db.refresh(project)
    return project
