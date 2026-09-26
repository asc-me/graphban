"""Write the capture job files into $DC_WORK. Ports match run.sh."""
import json, os, pathlib, tempfile

W = pathlib.Path(os.environ.get("DC_WORK", pathlib.Path(tempfile.gettempdir()) / "gb-design-capture"))
W.mkdir(parents=True, exist_ok=True)
SELF = os.environ.get("DC_SELF_WEB", "http://localhost:5199")
HOSTED = os.environ.get("DC_HOSTED_WEB", "http://localhost:5198")
ALEX = {"email": "alex@ascme-labs.com", "password": "graphban"}  # app.seed.SEED_PASSWORD


def dump(name, base, jobs, login=ALEX):
    json.dump({"base": base, **(login or {}), "jobs": jobs}, open(W / name, "w"))


# Self-host, seeded: the populated variant of every flat route.
routes = {
    "home": "/home", "tracker": "/tracker", "requests": "/requests", "triage": "/triage", "prds": "/prds",
    "roadmap": "/roadmap", "links": "/links", "code": "/code", "fleet-v2": "/fleet.v2", "fleet-v1": "/fleet.v1",
    "outposts": "/outposts", "harness": "/harness", "memory-review": "/memory-review", "lessons": "/lessons",
    "activity": "/activity", "live": "/live",
    "settings-providers": "/settings/deployment/providers", "settings-gitops": "/settings/deployment/gitops",
    "settings-updates": "/settings/deployment/updates", "settings-sync": "/settings/deployment/sync",
    "settings-project": "/settings/project", "settings-members": "/settings/project/members",
    "settings-mcp": "/settings/project/mcp", "settings-feedback-kit": "/settings/project/feedback-kit",
    "settings-integrations": "/settings/project/integrations", "settings-api-keys": "/settings/project/api-keys",
    "settings-account": "/settings/account", "profile": "/profile",
}
dump("jobs-self.json", SELF, [{"name": k, "path": v, "variant": "populated"} for k, v in routes.items()])

# Hosted: fresh project GRPH (the empty variants) plus org, org admin, operator, hosted settings.
views = ["tracker", "requests", "triage", "prds", "roadmap", "links", "code", "fleet.v2", "fleet.v1", "outposts",
         "harness", "memory-review", "lessons", "activity", "live"]
hosted = [{"name": "h-" + v.replace(".", "-"), "path": "/p/GRPH/" + v, "variant": "empty"} for v in views]
hosted.append({"name": "h-project-home", "path": "/p/GRPH", "variant": "populated"})
for k, v in {
    "org": "/org", "galaxy": "/org/galaxy", "orgadmin-users": "/org/admin/users", "orgadmin-teams": "/org/admin/teams",
    "orgadmin-deployments": "/org/admin/deployments", "orgadmin-integrations": "/org/admin/integrations",
    "orgadmin-gitops": "/org/admin/gitops", "orgadmin-billing": "/org/admin/billing", "op-home": "/admin",
    "op-orgs": "/admin/orgs", "op-users": "/admin/users", "op-licensing": "/admin/licensing",
    "settings-sync": "/settings/deployment/sync", "settings-providers": "/settings/deployment/providers",
    "settings-project": "/settings/project", "settings-api-keys": "/settings/project/api-keys",
    "settings-account": "/settings/account", "profile": "/profile",
}.items():
    hosted.append({"name": "h-" + k, "path": v, "variant": "populated"})
dump("jobs-hosted.json", HOSTED, hosted)

# Self-host: open overlays, then loading / error for the pages that have them.
extra = [
    {"name": "tracker-item-open", "path": "/tracker", "variant": "populated", "clicks": ["text=Wire AI chat sidebar"]},
    {"name": "tracker-new-item", "path": "/tracker", "variant": "populated", "clicks": ["button:has-text('New item')"]},
    {"name": "prd-editor", "path": "/prds", "variant": "populated", "clicks": ["text=Graphban Core MVP"]},
    {"name": "command-palette", "path": "/home", "variant": "populated", "keys": ["Meta+k"]},
    {"name": "requests-open", "path": "/requests", "variant": "populated", "clicks": ["text=Memory search returns stale"]},
    {"name": "chat-tab", "path": "/home", "variant": "populated", "clicks": ["button:has-text('Chat')"]},
]
for v in ["home", "tracker", "requests", "prds", "roadmap", "fleet.v2", "harness", "lessons", "memory-review", "code",
          "triage", "live"]:
    for var in ["loading", "error"]:
        extra.append({"name": f"{v.replace('.', '-')}--{var}", "path": "/" + v, "variant": var})
dump("jobs-extra.json", SELF, extra)

# Component markup lifted from live pages, plus the Fleet.v1 tabs.
dump("jobs-x.json", SELF, [
    {"name": "x-tracker", "path": "/tracker", "variant": "populated", "extract": {
        "btn-primary": "button:has-text('New item')",
        "filter-chips": "main button:has-text('Backlog'), main button:has-text('Blocked'), main button:has-text('Active')",
        "search": "header input", "nav-item": "nav a", "tabs": "[role=tablist]", "row": "main [draggable], main li"}},
    {"name": "x-requests", "path": "/requests", "variant": "populated", "extract": {
        "type-badges": "main span:has-text('BUG'), main span:has-text('FEATURE')",
        "outline-btn": "main button:has-text('Publish'), main button:has-text('link')"}},
    {"name": "x-settings-project", "path": "/settings/project", "variant": "populated", "extract": {
        "input": "main input[type=text], main input:not([type])", "checkbox": "main label:has(input[type=checkbox])",
        "radio": "main label:has(input[type=radio])", "textarea": "main textarea"}},
    {"name": "x-harness", "path": "/harness", "variant": "populated", "extract": {
        "select": "main select", "btn-primary": "main button:has-text('Save')"}},
    {"name": "x-newitem", "path": "/tracker", "variant": "populated", "clicks": ["button:has-text('New item')"],
     "extract": {"dialog": "[role=dialog]"}},
    {"name": "x-home", "path": "/home", "variant": "populated", "extract": {
        "cards": "main a:has-text('Triage'), main a:has-text('Code graph')"}},
    {"name": "fleet-v1-wave", "path": "/fleet.v1", "variant": "populated", "clicks": ["main button:has-text('Wave')"]},
    {"name": "fleet-v1-work", "path": "/fleet.v1", "variant": "populated", "clicks": ["main button:has-text('Work')"]},
])

# Unauthed. The public :token pages get a dummy token, so they capture their real not-found state.
dump("jobs-unauth.json", SELF, [
    {"name": "login", "path": "/", "variant": "populated"},
    {"name": "signup", "path": "/", "variant": "populated", "clicks": ["button:has-text('Sign up'), button:has-text('Create')"]},
    {"name": "reset-password", "path": "/reset-password?token=x", "variant": "populated"},
    {"name": "invite-accept", "path": "/invite/bad-token", "variant": "populated"},
    {"name": "public-roadmap", "path": "/public/bad/roadmap", "variant": "populated"},
    {"name": "public-feedback", "path": "/public/bad/feedback", "variant": "populated"},
    {"name": "public-issues", "path": "/public/bad/issues", "variant": "populated"},
    {"name": "public-tracking", "path": "/track/bad", "variant": "populated"},
    {"name": "embed-feedback", "path": "/embed/feedback?project=core", "variant": "populated"},
    {"name": "embed-roadmap", "path": "/embed/roadmap?project=core", "variant": "populated"},
], login=None)

# Hosted onboarding: sam has no org yet, rui has an org but no project (bootstrap.sh makes both).
dump("jobs-onboard-org.json", HOSTED, [{"name": "onboarding-create-org", "path": "/", "variant": "populated"}],
     login={"email": "sam@example.com", "password": "graphban1"})
dump("jobs-onboard-project.json", HOSTED, [{"name": "onboarding-create-project", "path": "/", "variant": "populated"}],
     login={"email": "rui@example.com", "password": "graphban1"})
print("jobs written to", W)
