"""`graphban` — a thin local CLI over the code-graph sync services (AL-218 / AL-134 D4).

Drives the AL-137/139/140 sync **directly against the local instance's database**, so a
self-host operator can link, push, purge, and move code-graph bundles with one command
instead of raw HTTP. (The HTTP sync endpoints don't accept the cloud credential in their
body — the `code_sync` service functions do — so the CLI calls the services, not the API.)

Run it where `DATABASE_URL` points at your instance — inside the backend container
(`docker compose exec backend graphban sync`) or with the env exported. The cloud link
(URL + org-issued sync credential) is stored in `~/.graphban/config.json`, chmod 600. An
existing `~/.agentledger/config.json` is still read if the new one is absent (override
either with `GRAPHBAN_CONFIG`, or the older `AGENTLEDGER_CONFIG`).

    graphban link --cloud-url https://cloud.example/ --api-key gb_sk_… --project core
    graphban status          # link + last-synced state
    graphban sync            # incremental push of the linked project's code graph
    graphban purge --yes     # delete this project's graph from the cloud
    graphban export --out graph.json
    graphban import --in graph.json --prune
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


# Config locations in preference order, new name first (AL-262). An operator who linked
# under the old name keeps working with no action; their file is read where it lies and
# is never moved or deleted, because it is theirs and it holds a live credential.
def _config_candidates() -> list[Path]:
    explicit = os.environ.get("GRAPHBAN_CONFIG") or os.environ.get("AGENTLEDGER_CONFIG")
    if explicit:
        return [Path(explicit)]
    return [
        Path.home() / ".graphban" / "config.json",
        Path.home() / ".agentledger" / "config.json",
    ]


def _read_path() -> Path:
    """Where to read from: the first candidate that exists, else the preferred one."""
    candidates = _config_candidates()
    return next((p for p in candidates if p.exists()), candidates[0])


def _write_path() -> Path:
    """Where to write: the new location (AL-263). An existing `~/.agentledger/config.json`
    is still READ and is never moved or deleted — it is the operator's file and it holds a
    live credential, so removing it is their call, not ours."""
    explicit = os.environ.get("GRAPHBAN_CONFIG") or os.environ.get("AGENTLEDGER_CONFIG")
    return Path(explicit) if explicit else Path.home() / ".graphban" / "config.json"


def _config_path() -> Path:
    """Back-compat shim for callers that only want to name the file."""
    return _read_path()


def load_config() -> dict:
    path = _read_path()
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        sys.exit(f"graphban: config at {path} is not valid JSON ({e})")


def save_config(cfg: dict) -> Path:
    path = _write_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    path.chmod(0o600)  # holds the sync credential
    return path


def _project(args, cfg: dict) -> str:
    return getattr(args, "project", None) or cfg.get("project") or "core"


def _session():
    from app.db import SessionLocal
    return SessionLocal()


# ---- commands -----------------------------------------------------------------

def cmd_init(args) -> int:
    """First-run provisioning (AL-283). Prints JSON so `start.sh` can consume it —
    the credential has to reach the HOST, where the MCP client runs, and this command
    executes inside the container."""
    from app import bootstrap

    db = _session()
    try:
        kwargs = {"project_name": args.project_name, "name": args.operator_name,
                  "key_scope": args.key_scope, "key_tiers": args.key_tiers}
        if args.email:
            kwargs["email"] = args.email
        result = bootstrap.provision(db, **kwargs)
    except bootstrap.BootstrapRefused as e:
        sys.exit(f"graphban init: {e}")
    finally:
        db.close()

    if args.json:
        print(json.dumps(result))
        return 0
    if not result["provisioned"]:
        print(f"Nothing to do — {result['reason']}.")
        return 0
    print(f"Provisioned project {result['project_name']} ({result['project_tag']}).")
    print(f"Sign in as {result['email']} / {result['password']}")
    print(f"API key: {result['api_key']}")
    if result.get("key_scope") == "global":
        print("Global key — MCP calls must pass project_id (or fall back to the default project).")
    else:
        print(f"Project-scoped key — the agent's writes default to {result['project_name']}.")
    tiers = result.get("key_tiers") or []
    if tiers:
        print(f"Key tiers: {', '.join(tiers)} — those tools appear in the agent's manifest.")
    else:
        print("Key tiers: none (core-only manifest). Tiered tools dispatch but are not advertised.")
    print("This key is stored only as a hash — it cannot be shown again.")
    return 0


def cmd_link(args) -> int:
    """Link this instance to a cloud tenant, recording it in BOTH places (AL-281).

    The config file is what the CLI's own commands read. The `sync_link` row is what
    everything server-side reads — `code_sync.link_status()` resolves that row, then the
    env link, and never consults the config file. Writing only the file left a
    CLI-linked box reporting `linked: false` to the server, which would make the AL-284
    authority gate FAIL OPEN: an agent could create projects that reach the org's tenant
    space precisely on the instances that are linked to one.

    The DB write is required, not best-effort. A silent failure here is the fail-open
    case, and this CLI already runs where `DATABASE_URL` points at the instance.
    """
    from app.services import code_sync

    cfg = load_config()
    if args.cloud_url:
        cfg["cloud_url"] = args.cloud_url.rstrip("/")
    if args.api_key:
        cfg["api_key"] = args.api_key
    if args.project:
        cfg["project"] = args.project
    if not cfg.get("cloud_url") or not cfg.get("api_key"):
        sys.exit("graphban link: need --cloud-url and --api-key (the org-issued sync credential)")

    db = _session()
    try:
        # Carry the existing org label through. `set_link` overwrites it, and the label
        # is the UI's to set — re-linking from the CLI must not blank it.
        existing = code_sync.get_link(db)
        org = args.org if getattr(args, "org", None) else (existing.org if existing else "")
        code_sync.set_link(db, cloud_url=cfg["cloud_url"], api_key=cfg["api_key"], org=org)
    except Exception as e:  # noqa: BLE001 — surface it; never leave the row unwritten
        sys.exit(
            f"graphban link: could not record the link in the database ({e}).\n"
            "The link is not saved. Run this where DATABASE_URL points at your instance "
            "(e.g. `docker compose exec api graphban link …`)."
        )
    finally:
        db.close()

    path = save_config(cfg)
    print(f"Linked → {cfg['cloud_url']} (project {cfg.get('project', 'core')}). Saved to {path}.")
    return 0


def cmd_status(args) -> int:
    from app.services import code_sync

    cfg = load_config()
    db = _session()
    try:
        server = code_sync.link_status(db)
    finally:
        db.close()

    if not cfg.get("cloud_url"):
        if server["linked"]:
            # The server is linked but this config isn't — sync from here would not know
            # where to push. Say so rather than reporting a bare "not linked".
            print(f"Not linked in {_read_path()}, but the INSTANCE is linked to "
                  f"{server['cloud_url']} (source: {server['source']}).")
            print("Run: graphban link --cloud-url … --api-key … to link this config too.")
            return 0
        print("Not linked. Run: graphban link --cloud-url … --api-key …")
        return 0
    project = _project(args, cfg)
    key = cfg.get("api_key", "")
    print(f"Linked to : {cfg['cloud_url']}")
    print(f"Project   : {project}")
    print(f"Credential: {'set (' + key[:6] + '…)' if key else 'MISSING'}")
    # The two records must agree — a mismatch is what AL-281 exists to make impossible,
    # so surface it loudly rather than letting the server-side gate read the other one.
    if not server["linked"]:
        print("WARNING   : the instance has NO link recorded. Re-run `graphban link`.")
    elif server["cloud_url"] != cfg["cloud_url"]:
        print(f"WARNING   : the instance is linked to {server['cloud_url']} "
              f"(source: {server['source']}) — this config disagrees.")

    from app.models import CodeSyncState
    db = _session()
    try:
        state = db.get(CodeSyncState, project)
    finally:
        db.close()
    if state is None:
        print("Last sync : never")
    else:
        print(f"Last sync : {state.last_synced_at} · {len(state.manifest or {})} paths in the pushed manifest")
    return 0


def cmd_sync(args) -> int:
    cfg = load_config()
    from app.services import code_sync
    db = _session()
    try:
        result = code_sync.push(
            db, project_id=_project(args, cfg),
            cloud_url=cfg.get("cloud_url", ""), api_key=cfg.get("api_key", ""),
        )
    except code_sync.NotLinked as e:
        sys.exit(f"graphban sync: {e}  (run `graphban link` first)")
    finally:
        db.close()
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_purge(args) -> int:
    if not args.yes:
        sys.exit("graphban purge: this deletes the project's graph from the cloud. Re-run with --yes.")
    cfg = load_config()
    from app.services import code_sync
    db = _session()
    try:
        result = code_sync.purge(
            db, project_id=_project(args, cfg),
            cloud_url=cfg.get("cloud_url", ""), api_key=cfg.get("api_key", ""),
        )
    except code_sync.NotLinked as e:
        sys.exit(f"graphban purge: {e}")
    finally:
        db.close()
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_export(args) -> int:
    cfg = load_config()
    project = _project(args, cfg)
    from app.services import code_graph
    db = _session()
    try:
        graph = code_graph.export_graph(db, project)
    finally:
        db.close()
    bundle = {"bundle_version": 1, "project_id": project,
              "nodes": graph.get("nodes", []), "edges": graph.get("edges", [])}
    text = json.dumps(bundle, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text + "\n")
        print(f"Exported {len(bundle['nodes'])} nodes / {len(bundle['edges'])} edges → {args.out}")
    else:
        print(text)
    return 0


def cmd_import(args) -> int:
    cfg = load_config()
    try:
        bundle = json.loads(Path(args.infile).read_text())
    except FileNotFoundError:
        sys.exit(f"graphban import: no such bundle: {args.infile}")
    except json.JSONDecodeError as e:
        sys.exit(f"graphban import: {args.infile} is not valid JSON ({e})")
    project = args.project or bundle.get("project_id") or cfg.get("project") or "core"
    from app.services import code_graph
    db = _session()
    try:
        result = code_graph.describe_code(
            db, project_id=project,
            nodes=bundle.get("nodes", []), edges=bundle.get("edges", []), prune=args.prune,
        )
    finally:
        db.close()
    print(json.dumps({"project_id": project, **result}, indent=2, default=str))
    return 0


def cmd_admin_bootstrap_hosted(args) -> int:
    """Create the first operator and org on a HOSTED instance (GRPH-219).

    Run it inside the deployed service — `railway ssh --service backend graphban admin
    bootstrap-hosted …` — because having shell access there IS the authority proof. It
    mints no API key and creates no project: everything past the first login goes through
    the product.
    """
    from app import bootstrap

    db = _session()
    try:
        result = bootstrap.provision_hosted(
            db, email=args.email, org_name=args.org_name, name=args.operator_name)
    except bootstrap.BootstrapRefused as e:
        sys.exit(f"graphban admin bootstrap-hosted: {e}")
    finally:
        db.close()

    if args.json:
        print(json.dumps(result))
        return 0
    if not result["provisioned"]:
        print(f"Nothing to do — {result['reason']}.")
        return 0
    print(f"Created org {result['org_name']} ({result['org_id']}).")
    print(f"Sign in as {result['email']} / {result['password']}")
    print("Change that password after signing in — it is shown here once and never again.")
    return 0


def cmd_learn(args) -> int:
    """Run the learning loop against this instance's database (GRPH-353 / PRD-16).

    The CLI half of the driver. It exists alongside `POST /api/learning/run` because they
    suit different deployments and both are real: a self-host operator following the
    README's local-Docker-first path schedules

        docker compose exec api graphban learn run --stage ingest

    from cron and needs no network or credential, while a hosted instance points its
    scheduler at the route. Both call `learning.run`, so there is one implementation of what
    a run actually does.
    """
    from app.services import learning as learning_svc

    db = _session()
    try:
        result = learning_svc.run(db, stage=args.stage, project_id=args.project or "core",
                                  limit_sources=args.limit_sources)
    except learning_svc.UnknownStage as e:
        sys.exit(f"graphban learn: {e}")
    finally:
        db.close()
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_eval(args) -> int:
    """Run golden-set evals for generative surfaces (GRPH-224).

    Mechanical checks always run (shape, must/must-not). `--judge` asks the
    project's chat model; without it, and on the stub, the judge half is
    ungraded rather than a pass. Exit 2 if no cases were found — an empty
    directory must not look like a green run.
    """
    from app.services import evals as evals_svc

    db = _session()
    try:
        result = evals_svc.run(
            db,
            surface=None if args.surface == "all" else args.surface,
            judge=args.judge,
            root=Path(args.dir) if args.dir else None,
        )
    except evals_svc.UnknownSurface as e:
        sys.exit(f"graphban eval: {e}")
    except ValueError as e:
        sys.exit(f"graphban eval: {e}")
    finally:
        db.close()
    print(json.dumps(result, indent=2, default=str))
    if result["status"] == "absent":
        return 2
    return 0 if result["status"] == "ok" else 1


def cmd_learn_inventory(args) -> int:
    """Inventory the artifacts installed on THIS machine (GRPH-354 / PRD-16).

    The scan has to run where the files are. A server-side walk would find nothing under
    `hosted_mode` and nothing inside the compose container either — and it would report a
    population of zero without erroring, which is the exact failure the inventory exists to
    close.

    Two ways to deliver the findings, because two deployments need different things:

      --api-url    POST them to an instance (the hosted path, and the right one when this
                   runs on a laptop that has no database credentials).
      otherwise    write straight to the database this process can already see.

    Reads only. Nothing on disk is written, moved or deleted by this command under any input.
    """
    from app.services import artifact_inventory as inv_svc

    roots = args.root or ["~/.claude"]
    out = {"roots": {}}
    for raw in roots:
        found, stats = inv_svc.scan([raw])
        items = [d.as_dict() for d in found]
        if args.api_url:
            result = _post_inventory(args, raw, items)
        else:
            db = _session()
            try:
                result = inv_svc.record_scan(
                    db, project_id=args.project or "core", root=raw, items=items)
            finally:
                db.close()
        out["roots"][raw] = {"scan": stats, "recorded": result}
    print(json.dumps(out, indent=2, default=str))
    return 0


def _post_inventory(args, root: str, items: list[dict]) -> dict:
    import httpx

    cfg = load_config()
    key = args.api_key or cfg.get("api_key", "")
    if not key:
        sys.exit("graphban learn inventory: --api-url needs --api-key (or a linked config)")
    url = args.api_url.rstrip("/") + "/api/artifacts/inventory"
    body = {"root": root, "items": items, "project_id": args.project}
    try:
        r = httpx.post(url, json=body, headers={"X-API-Key": key}, timeout=30)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001 — a failed post must say so, not report success
        sys.exit(f"graphban learn inventory: could not post to {url} ({e})")
    return r.json()


def _comma_list(value: str) -> list[str]:
    """`--key-tiers prd,fleet` and `--key-tiers prd --key-tiers fleet` both land on a list."""
    return [t.strip() for t in value.split(",") if t.strip()]


def build_parser() -> argparse.ArgumentParser:
    from app import bootstrap  # for KEY_SCOPES — one source of truth for the scope names
    from app.services import tool_tiers  # for TIERS — same, for the tier names

    p = argparse.ArgumentParser(
        prog="graphban", description="Local code-graph sync for a Graphban self-host (AL-134).")
    sub = p.add_subparsers(dest="command", required=True)

    it = sub.add_parser("init", help="first-run provisioning: operator, project, and one key")
    it.add_argument("--project-name", default="My Project")
    it.add_argument("--email", default=None, help="operator sign-in address")
    it.add_argument("--operator-name", default="Operator")
    it.add_argument("--key-scope", choices=bootstrap.KEY_SCOPES, default="project",
                    help="scope of the provisioned MCP key: project (default) binds it to the "
                         "new project so the agent's writes target it; global leaves it "
                         "unbound — calls pass project_id per call (or fall back to the "
                         "default project)")
    it.add_argument("--key-tiers", type=_comma_list, action="extend", default=None,
                    metavar="TIERS",
                    help="comma-separated optional tool tiers advertised in the key's "
                         f"manifest ({', '.join(tool_tiers.TIERS)}); repeatable; default is "
                         "core-only. Visibility, not authorisation")
    it.add_argument("--json", action="store_true", help="machine-readable output for start.sh")
    it.set_defaults(func=cmd_init)

    lk = sub.add_parser("link", help="store the cloud sync target (URL + org-issued credential)")
    lk.add_argument("--cloud-url")
    lk.add_argument("--api-key")
    lk.add_argument("--project")
    lk.add_argument("--org", help="optional label for the linked org (shown in the UI); "
                                  "omitted keeps whatever label is already recorded")
    lk.set_defaults(func=cmd_link)

    st = sub.add_parser("status", help="show the link + last-synced state")
    st.add_argument("--project")
    st.set_defaults(func=cmd_status)

    sy = sub.add_parser("sync", help="incremental push of the project's code graph to the cloud")
    sy.add_argument("--project")
    sy.set_defaults(func=cmd_sync)

    pu = sub.add_parser("purge", help="delete the project's code graph from the cloud")
    pu.add_argument("--project")
    pu.add_argument("--yes", action="store_true", help="confirm the destructive purge")
    pu.set_defaults(func=cmd_purge)

    ex = sub.add_parser("export", help="write the project's code graph as a portable bundle")
    ex.add_argument("--project")
    ex.add_argument("--out", help="output file (default: stdout)")
    ex.set_defaults(func=cmd_export)

    im = sub.add_parser("import", help="import a code-graph bundle into the project (re-embeds locally)")
    im.add_argument("--in", dest="infile", required=True, metavar="FILE")
    im.add_argument("--project")
    im.add_argument("--prune", action="store_true", help="mark paths absent from the bundle stale")
    im.set_defaults(func=cmd_import)

    ad = sub.add_parser("admin", help="operator actions that cannot go through the product")
    adsub = ad.add_subparsers(dest="admin_command", required=True)
    bh = adsub.add_parser(
        "bootstrap-hosted",
        help="create the first operator + org on a hosted instance (no API key minted)")
    bh.add_argument("--email", required=True,
                    help="operator sign-in address; must be in PLATFORM_ADMIN_EMAILS or the "
                         "account cannot reach the operator console")
    bh.add_argument("--org-name", required=True, dest="org_name")
    bh.add_argument("--operator-name", default="Operator")
    bh.add_argument("--json", action="store_true")
    bh.set_defaults(func=cmd_admin_bootstrap_hosted)

    ln = sub.add_parser("learn", help="run the learning loop (transcript ingest → artifacts)")
    lnsub = ln.add_subparsers(dest="learn_command", required=True)
    lr = lnsub.add_parser("run", help="ingest transcripts and/or draft artifacts")
    # Spelled out rather than imported from `learning.STAGES`: building the parser happens
    # on every invocation including `--help`, and importing the service layer there drags in
    # SQLAlchemy and the whole app to print a usage string. `test_cli_learn_stages_match`
    # pins these against STAGES so the duplication cannot drift.
    lr.add_argument("--stage", default="all", choices=["ingest", "artifacts", "all"],
                    help="`ingest` mines transcripts into candidates; `artifacts` turns "
                         "PUBLISHED lessons into recommendations. They sit either side of "
                         "human triage, so the two run on the same schedule and the second "
                         "picks up whatever was approved since the last pass.")
    lr.add_argument("--project", help="project to attribute mined evidence to (default core)")
    lr.add_argument("--limit-sources", type=int, default=None, dest="limit_sources",
                    help="stop after N transcripts — for a first look at a large archive")
    lr.set_defaults(func=cmd_learn)

    li = lnsub.add_parser("inventory",
                          help="record which artifacts are installed on this machine")
    li.add_argument("--root", action="append",
                    help="directory to scan; repeatable (default ~/.claude). Orphaning is "
                         "scoped per root, so scanning one never marks another's missing.")
    li.add_argument("--project", help="project to attribute the inventory to (default core)")
    li.add_argument("--api-url", help="post findings to this instance instead of writing "
                                      "to the local database (the hosted path)")
    li.add_argument("--api-key", help="credential for --api-url; falls back to the linked "
                                      "config")
    li.set_defaults(func=cmd_learn_inventory)

    # Spelled out rather than imported from `evals.SURFACES`: `--help` must not
    # import the service layer. `test_evals` pins these against SURFACES.
    ev = sub.add_parser("eval", help="run golden-set evals for generative surfaces")
    ev.add_argument("--surface", default="all",
                    choices=["extract_lessons", "all"],
                    help="one surface, or all registered surfaces (default all)")
    ev.add_argument("--judge", action="store_true",
                    help="ask the project's chat model; stub stays ungraded")
    ev.add_argument("--dir", help="override the cases directory (default: app/evals/cases)")
    ev.set_defaults(func=cmd_eval)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
