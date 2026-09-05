from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The insecure fallback secret. Anyone knowing it can mint tokens for any user, so
# an internet-exposed deploy MUST override JWT_SECRET (see startup security check).
DEFAULT_JWT_SECRET = "dev-secret-change-me-in-production-0123456789abcdef"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Postgres by default; falls back to SQLite for zero-infra local runs / tests.
    database_url: str = "postgresql+psycopg://agentledger:agentledger@localhost:5432/agentledger"

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        """Managed hosts (Railway, Heroku, …) hand out ``postgres://…``; SQLAlchemy
        needs an explicit driver. Rewrite both ``postgres://`` and bare
        ``postgresql://`` to the psycopg3 driver so the provided URL just works,
        while ``sqlite://`` and already-qualified URLs pass through untouched (AL-26)."""
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix) and not v.startswith("postgresql+"):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v

    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 30
    refresh_token_days: int = 14

    # How long after a PR is linked an item must wait before it can be completed
    # (GRPH-567, PRD-26 §PR cooldown). Long enough that CI has had time to run, so an
    # outcome cannot be recorded before the run it rests on exists.
    #
    # 600s is measured from THIS repository's own CI, not chosen for roundness: the two
    # backend jobs take 4m34s and 8m20s, so anything under about nine minutes can be
    # completed while the slower engine is still going. An install whose CI is faster
    # should lower it — the number is a property of the pipeline, not of the product.
    #
    # 0 disables it entirely, which is the right setting for a repository with no CI at
    # all: a delay protecting nothing is pure friction, and this must not become a tax on
    # every completion.
    pr_cooldown_seconds: int = 600

    # Auth hardening (AL-72). Login is rate-limited per-email and (more loosely)
    # per-IP to blunt credential stuffing / brute force.
    login_rate_per_min: int = 10
    min_password_length: int = 8

    # Who may create an account (AL-93). Replaces the old open_registration boolean,
    # which couldn't express "closed to the public, open to invite holders" — the
    # exact shape a private beta needs:
    #   open        — anyone may sign up (self-host default, today's behavior)
    #   invite_only — signup requires a valid platform or org invite
    #   closed      — no signup at all, invite or not
    signup_mode: str = "open"

    @field_validator("signup_mode")
    @classmethod
    def _valid_signup_mode(cls, v: str) -> str:
        allowed = {"open", "invite_only", "closed"}
        v = (v or "").strip().lower()
        if v not in allowed:
            raise ValueError(f"signup_mode must be one of {sorted(allowed)}")
        return v

    # Security hardening for internet-exposed deploys (all safe-by-default for local):
    # verify inbound GitHub webhook HMAC when set; trust X-Forwarded-For only behind a
    # known proxy; refuse to start on a weak/default JWT secret when required.
    github_webhook_secret: str = ""
    trusted_proxy: bool = False
    # How many proxies stand between this app and the internet (GRPH-553). 0 = none; the
    # socket peer is the caller.
    #
    # Supersedes `trusted_proxy`, which reads the FIRST `X-Forwarded-For` hop — the one value
    # a client fully controls, since the bundled nginx APPENDS rather than overwrites. This
    # counts from the RIGHT, so it only ever reads what a proxy actually observed: 1 for the
    # compose stack (client -> nginx -> app), 2 behind an edge that already sets the header
    # (client -> edge -> nginx -> app).
    #
    # Not a bool because the two topologies differ by one hop and a bool cannot say which —
    # that is precisely how the same nginx template ended up correct on Railway and wrong on
    # the self-host.
    trusted_hops: int = 0
    require_strong_secret: bool = False

    # Release identity: the git revision this image was built from, baked in at
    # `docker compose build` time (see docs/deploy.md) and reported by /health.
    #
    # The default only applies when the variable is ABSENT. Railway defines `GIT_SHA` and
    # resolves it from `RAILWAY_GIT_COMMIT_SHA`, which the platform supplies **only for
    # GitHub-triggered deploys** — so a redeploy started any other way (a variable change,
    # a manual restart) sets it to the empty string, which wins over this default and makes
    # `/health` answer `"git_sha": ""`.
    #
    # An empty answer is worse than an honest "unknown": `ok` with a blank revision reads as
    # a health check that has nothing to say, when what happened is that it could not find
    # out. `resolved_git_sha` is what `/health` reports, so absence stays legible (GRPH-426).
    git_sha: str = "unknown"

    # Railway injects this into the container for a repo-connected service. It is NOT in the
    # service's referenceable variable set — `${{ RAILWAY_GIT_COMMIT_SHA }}` resolves to
    # empty even on a push-triggered deploy, which is how `GIT_SHA` stayed blank after being
    # pointed at it. Read directly instead of through the resolver. Empty everywhere else,
    # which costs nothing: it is only ever a fallback.
    railway_git_commit_sha: str = ""

    @property
    def resolved_git_sha(self) -> str:
        """The revision, or `unknown` — never blank.

        `GIT_SHA` first, because a self-host bakes it in at build time and that is the
        deliberate answer. Railway's injected commit second, so the hosted instance can say
        what it is without an operator remembering to set anything. `unknown` last, because
        an absent identity must not read as a clean one (GRPH-426).
        """
        # `unknown` is a SENTINEL, not a revision, and it has to be treated as absence here
        # or it shadows everything after it. The Dockerfile bakes `ARG GIT_SHA=unknown` into
        # the image, so on Railway the container really does carry that literal — and a
        # fallback placed after it would never be reached. Found by the case table below
        # rather than in production, which is the only reason this reads correctly.
        for candidate in (self.git_sha, self.railway_git_commit_sha):
            value = (candidate or "").strip()
            if value and value != "unknown":
                return value
        return "unknown"

    # Secret encryption at rest (AL-73). When set, BYOK provider API keys (and other
    # stored secrets) are Fernet-encrypted in the DB; unset means store as-is (fine for
    # a trusted single-tenant self-host). Any string works — it's stretched to a key.
    # Hosted mode requires it (check_security refuses to boot otherwise).
    secret_encryption_key: str = ""

    # Multi-tenant SaaS switch. OFF for self-hosted/OSS builds (flat User→Project,
    # cross-project "global" memory allowed). ON only for the hosted offering, where
    # Organizations + billing + quotas mount and tenant boundaries tighten (e.g. no
    # project-less global shards, so one tenant's memory can never reach another).
    hosted_mode: bool = False

    # Plan/quota administration (AL-75). During private beta, org plans are assigned
    # MANUALLY by an operator. Stripe self-serve (GRPH-82) lights up only when the
    # four STRIPE_* keys below are set; unset keeps this path as the only writer.
    # Only accounts whose email is in this comma-separated allowlist may change an
    # org's plan by hand — an org owner can't upgrade their own org for free.
    platform_admin_emails: str = ""

    # Self-serve billing (GRPH-82). Empty = manual assignment only. All four must
    # be set before checkout, the portal, or the webhook do anything.
    stripe_api_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_pro: str = ""
    stripe_price_team: str = ""

    @property
    def platform_admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.platform_admin_emails.split(",") if e.strip()}

    embed_dim: int = 384

    # LLM/embedding call budget. A gateway behind an edge proxy (e.g. Cloudflare) will
    # cut a request off at roughly 100s to FIRST byte, so keep the ceiling under that:
    # a cold model should fail here with a clear error rather than be severed upstream.
    # Streaming chat is unaffected by that limit — bytes start flowing immediately —
    # which is why chat() assembles from the stream (see providers/openai_compat.py).
    llm_timeout_seconds: float = 90.0
    # Transient blips and cold starts get a retry before ingest degrades to "no vector".
    embed_max_retries: int = 2

    # Hosted deployments should embed with a real provider: on `stub`, vectors are
    # deterministic noise, so search silently returns nonsense while looking healthy.
    # Startup warns loudly; set this to refuse to boot instead.
    require_real_embeddings: bool = False
    # There is deliberately no REQUIRE_REAL_CHAT counterpart — see security/startup.py.
    # Embeddings are an instance-wide, deploy-time choice (the vector dimension is baked
    # into the schema); chat is per-project BYOK resolved from the DB, so no env flag can
    # describe it.

    # ---- AI providers (F1). Defaults are all-stub → fully offline. ----
    # embed_provider: stub | ollama | openai. EMBED_DIM MUST match the model's output
    # width — a mismatch is only caught when a vector is written:
    #   stub-384 = 384 · nomic-embed-text = 768 · bge-m3 = 1024 · text-embedding-3-small = 1536
    # Changing embed_dim on a populated database means resizing the vector columns and
    # re-embedding everything (see migration 0019 + /api/memory/backfill), so pick it
    # before real data lands.
    embed_provider: str = "stub"
    # chat_provider — LEGACY MIRROR, not the resolution path. `platform.apply_llm` writes
    # this at runtime to keep the old `llm_mode` field working; the live chat provider is
    # `PlatformConfig.active_chat_provider`, per project, in the DB (see the provider
    # registry for the full set — anthropic, openai, xai, gemini, groq, deepseek, mistral,
    # ollama). Setting CHAT_PROVIDER in the environment does NOT select a chat provider.
    chat_provider: str = "stub"

    ollama_base_url: str = "http://localhost:11434"
    ollama_embed_model: str = "nomic-embed-text"
    ollama_chat_model: str = "llama3.1:8b"
    ollama_auth_key: str = ""  # optional bearer for a Caddy-guarded public Ollama endpoint
    # How long Ollama should hold the model resident after a call. Empty means "say
    # nothing", which leaves the server's own default (5m) in charge — the behaviour every
    # install had before this existed, so nobody's memory policy changes by upgrading.
    #
    # Worth setting because Graphban's calls are BURSTY in a way a generic default cannot
    # know: a grill round, then a minute while the author types, then another round. On
    # ms-s1-ubt that gap expires the 5m window and the next call pays a measured 9.7s to
    # reload mistral-small3.1:24b — 9.7s of a ~16s grill spent on a model that was resident
    # a moment earlier. Accepts Ollama's own forms: "30m", "1h", seconds as a number, or
    # "-1" to pin indefinitely. Pinning costs the model's full weight in RAM (15GB here),
    # which is why this is the operator's call and not a default.
    ollama_keep_alive: str = ""

    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_embed_model: str = "text-embedding-3-small"

    # Anthropic auth is read from ANTHROPIC_API_KEY by the SDK.
    anthropic_model: str = "claude-opus-4-8"

    # Public embeddable feedback form (Phase 2).
    public_submit_enabled: bool = True

    # Upstream feedback: where a "Report an issue with Graphban" report is forwarded
    # (always user/agent-initiated — never silent telemetry). Defaults to ASCME's hosted
    # intake; a deployer can repoint it, or set the URL blank to disable the feature.
    upstream_feedback_url: str = "https://feedback.asc-me.dev/api/public/requests"
    # FROZEN until the upstream instance changes too: this is a project_id on the separate
    # feedback.asc-me.dev deployment, so renaming it here makes every issue report 404 on
    # arrival. It moves when a project with the new id exists there.
    #
    # Only consulted by a SELF-HOSTED upstream. `public._public_project` accepts a raw
    # project_id solely when the receiving instance is not in hosted mode; a hosted intake
    # takes the share token and nothing else, so this field is inert against one.
    upstream_feedback_project: str = "agentledger"
    # The receiving project's public share token — REQUIRED when the upstream runs in
    # hosted mode, which ignores project_id by design so one tenant cannot name another's
    # project (AL-73). Without it a hosted intake returns 404, deliberately made
    # indistinguishable from "no such project" so the surface cannot be probed. Mint it on
    # the upstream instance (project settings -> public sharing). Secret: set via
    # UPSTREAM_FEEDBACK_TOKEN in the environment, never committed here.
    upstream_feedback_token: str = ""

    # Local↔cloud hybrid (AL-137/139): where a LINKED local instance pushes its code graph,
    # and the org-minted 'sync'-scoped key it authenticates with. Blank = not linked — a pure
    # local-only tool that never reaches out to a cloud (the D2 default).
    sync_cloud_url: str = ""  # e.g. https://cloud.example.com
    sync_api_key: str = ""    # gb_sk_… with the 'sync' scope, minted org-side

    # Drive sync: base directory the filesystem backend syncs into. Mount this at a
    # Google Drive Desktop folder to reach Drive with no OAuth.
    sync_dir: str = "/data/sync"

    # Seed the design's dataset on startup when the DB is empty.
    seed_on_start: bool = True

    # How often the credential retry loop wakes (PRD-25 S2b). `0` disables it entirely — the
    # value the TEST suite runs at, because a background task that fires mid-test turns an
    # unrelated assertion into a flake and the loop has its own tests that drive it directly.
    # Production leaves it on: a credential saved while its provider was down is the whole
    # reason `pending_validation` exists, and nothing else re-asks.
    credential_retry_seconds: int = 60

    # LLM call spans (GRPH-225). How long `llm_call_spans` rows live; 0 or negative
    # keeps everything. The table is telemetry, not the ledger — unbounded growth of
    # per-call rows is the difference between a cost panel and an ops incident.
    llm_span_retention_days: int = 90
    # How long the observed feed (`agent_calls`, PRD-34 D10) keeps a row. Swept on the write
    # path, amortised — there is no scheduler here and this did not add one. `0` or negative
    # keeps everything.
    agent_call_retention_days: int = 7

    # Org invites (AL-74b). Delivered by email; when SMTP is unconfigured the email
    # service falls back to a console/outbox transport (fine for self-host + tests).
    # `app_base_url` is the SPA origin used to build the invite-accept link in the
    # email. `invite_expiry_days` bounds how long an emailed invite stays acceptable.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    # graphban.dev is verified in Resend (MX + SPF + DKIM + DMARC), so outbound mail now
    # comes from the new domain. agentldgr.dev stays verified and its hosts keep serving —
    # nothing that already went out needs to be reachable from a different address.
    smtp_from: str = "Graphban <no-reply@graphban.dev>"
    smtp_starttls: bool = True
    app_base_url: str = "http://localhost:5173"
    invite_expiry_days: int = 14

    # Rate limiting + observability (Phase 5). REDIS_URL, when set, backs rate limits
    # with a shared store so caps hold across multiple instances; unset keeps the
    # in-process limiter (fine for self-host / a single container / tests). The per-org
    # cap is a hosted burst limit on agent (MCP) calls, distinct from the monthly plan
    # quota (AL-75). Logging is structured text by default; LOG_JSON emits JSON lines.
    redis_url: str = ""
    org_rate_per_min: int = 300  # hosted per-org MCP burst cap (0 = disabled)
    log_level: str = "INFO"
    log_json: bool = False

    # Comma-separated list of allowed CORS origins for the SPA.
    cors_origins: str = "http://localhost:5173,http://localhost:8080"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # Predicate names an attestation must carry, PASSING, before an item may reach `done`
    # (GRPH-569, PRD-26 §Conformance and adversarial gates). Comma-separated, e.g.
    # `conformance,adversarial`.
    #
    # EMPTY BY DEFAULT, and that is the PRD's requirement rather than a concession: "an
    # install with only the CI adapter has an attestation carrying one predicate, and the
    # gate still functions". Requiring a predicate nobody emits would make completion
    # impossible on a working deployment — so this is required of the GATE'S CONFIGURATION,
    # not of every adapter, and an install turns it on when it has an adapter that answers it.
    required_predicates: str = ""

    @property
    def required_predicate_list(self) -> list[str]:
        return [p.strip() for p in self.required_predicates.split(",") if p.strip()]

    # Tool tiers every key gets on top of its own (GRPH-571). EMPTY BY DEFAULT — the whole
    # point of tiering is that the default manifest is smaller.
    #
    # This exists as the operator's undo. Tiering shrinks the manifest of keys that already
    # exist, since a key minted before 0093 has no tiers; setting this to
    # `prd,codegraph,fleet,misc` restores exactly the pre-GRPH-571 manifest for the whole
    # deployment, with no code change and no re-minting, for someone who wants the old
    # behaviour back while they work out which keys need what.
    #
    # Deliberately additive rather than a replacement: it can only widen what a key already
    # has, so turning it on can never take a tier away from a key that was minted with one.
    mcp_default_tool_tiers: str = ""

    @property
    def default_tool_tier_list(self) -> list[str]:
        return [t.strip() for t in self.mcp_default_tool_tiers.split(",") if t.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def jwt_secret_is_weak(self) -> bool:
        """The default secret, or anything too short to resist offline guessing."""
        return self.jwt_secret == DEFAULT_JWT_SECRET or len(self.jwt_secret) < 32


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
