"""Platform + integration config (Phase 5).

The AI-provider settings genuinely drive F1: updating llm_mode switches the live
chat/extraction provider (Ollama/Anthropic/stub). The embed provider stays a
deploy-time setting because changing it changes the pgvector column dimension.

GitHub/Drive here manage connection *state and config* — live OAuth/token exchange
and API sync are intentionally out of scope for the local slice (no third-party
credentials); the inbound GitHub webhook (routers/public.py) is fully implemented.
"""
from __future__ import annotations

from secrets import token_urlsafe

from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app import providers
from app.providers import probe
from app.providers import registry as provider_registry
from app.providers.base import ChatModel, Extractor
from app.models import PlatformConfig
from app.security import secrets


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


def resolve_chat(db: Session, project_id: str) -> tuple[str, ChatModel]:
    """(provider_id, chat model) for a specific project. Callers gate on the provider id
    (== "stub" → offline placeholder) and otherwise use the model. Per-project, so one
    project's provider never leaks into another's AI calls."""
    provider, base_url, api_key, model = _chat_params(get_config(db, project_id))
    return provider, providers.build_chat(provider, base_url=base_url, api_key=api_key, model=model)


def chat_model_for(db: Session, project_id: str) -> ChatModel:
    return resolve_chat(db, project_id)[1]


def extractor_for(db: Session, project_id: str) -> Extractor:
    provider, base_url, api_key, model = _chat_params(get_config(db, project_id))
    return providers.build_extractor(provider, base_url=base_url, api_key=api_key, model=model)


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
    return provider, providers.build_chat(provider, base_url=base_url, api_key=api_key, model=model)


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
