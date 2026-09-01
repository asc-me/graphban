"""The API serves the SPA, so a native install is one process (GRPH-577, PRD-27 S1).

In Docker, nginx serves the bundle and proxies `/api/` here. A native install has no reason
to pay for a second service — and nginx is where GRPH-523 came from, since it resolved the
backend address once at boot.

The mount is CONDITIONAL on `web/dist` existing, which means it does not run in this
repository's test tree. So these drive `_mount_spa` against a **fixture bundle** on a fresh
`FastAPI` app. A mount only ever exercised when a build happens to be present is one nobody
tests, and this is the slice where "it looked mounted" and "it served the right thing" are
different claims.

Every assertion below mirrors a line of `web/nginx.conf.template`, because two implementations
of one security posture is exactly the duplication this repository keeps finding.
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import (
    SPA_ASSET_CACHE_CONTROL,
    SPA_SECURITY_HEADERS,
    SPA_SHELL_CACHE_CONTROL,
    _mount_spa,
)

NGINX = pathlib.Path(__file__).resolve().parents[2] / "web" / "nginx.conf.template"


@pytest.fixture()
def bundle(tmp_path: pathlib.Path) -> pathlib.Path:
    """A built SPA, as `pnpm build` leaves it."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    (tmp_path / "version.txt").write_text("deadbee\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def client(bundle) -> TestClient:
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/items")
    def items():
        return {"items": []}

    _mount_spa(app, bundle)
    return TestClient(app)


# ---- the SPA itself ----------------------------------------------------------------------

def test_an_unknown_path_returns_the_app_shell(client):
    """A client-side route. The SPA router takes it from here."""
    r = client.get("/items/GRPH-1")
    assert r.status_code == 200
    assert "<div id=root>" in r.text


def test_a_built_asset_is_served(client):
    r = client.get("/assets/index-abc123.js")
    assert r.status_code == 200
    assert "console.log" in r.text


def test_a_file_beside_index_is_served(client):
    """`version.txt` is how the deploy script reads the web bundle's revision, so the API
    serving the SPA must serve it too or release identity stops being checkable."""
    r = client.get("/version.txt")
    assert r.status_code == 200
    assert r.text.strip() == "deadbee"


# ---- the two that are easy to get wrong ---------------------------------------------------

def test_a_missing_asset_is_404_and_never_the_app_shell(client):
    """THE ONE THAT MATTERS. nginx spells this `try_files $uri =404` deliberately.

    Falling back to index.html answers 200 with HTML where the browser asked for JavaScript.
    It surfaces as a MIME-type console error pointing at the wrong thing entirely — and a
    stale index.html naming a hashed bundle that no longer exists reads as a working deploy.
    """
    r = client.get("/assets/index-deleted.js")
    assert r.status_code == 404, "a missing asset fell through to the SPA"
    assert "<div id=root>" not in r.text


def test_an_unknown_api_path_is_json_not_the_app_shell(client):
    """An unmatched `/api/*` is a missing ENDPOINT. Returning the SPA hands an agent an HTML
    page where it expected an error object, and the 200 reads as success."""
    r = client.get("/api/definitely-not-a-route")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_real_routes_are_not_shadowed_by_the_catch_all(client):
    """The catch-all is mounted last so it cannot swallow a real endpoint. An ordering bug
    here turns an API route into an HTML page, and it reads as a frontend routing problem."""
    assert client.get("/api/items").json() == {"items": []}
    assert client.get("/health").json() == {"status": "ok"}


# ---- the headers, and that they survive an error ------------------------------------------

@pytest.mark.parametrize("header", sorted(SPA_SECURITY_HEADERS))
def test_the_security_headers_are_present(client, header):
    assert client.get("/").headers.get(header) == SPA_SECURITY_HEADERS[header]


@pytest.mark.parametrize("header", sorted(SPA_SECURITY_HEADERS))
def test_the_security_headers_survive_an_error_response(client, header):
    """nginx sets these with `always`, which applies them to errors too. Middleware that only
    decorated 2xx would drop them on exactly the responses an attacker can most easily
    provoke — and every test above this one would still pass."""
    r = client.get("/assets/index-deleted.js")
    assert r.status_code == 404
    assert r.headers.get(header) == SPA_SECURITY_HEADERS[header]


def test_the_header_set_matches_the_one_nginx_sends():
    """The shared posture, asserted against nginx's own config rather than a second list.

    Two implementations that drift is the failure here, and it drifts silently: the Docker
    path and the native path would each look correct in isolation.
    """
    conf = NGINX.read_text(encoding="utf-8")
    for name, value in SPA_SECURITY_HEADERS.items():
        assert f'add_header {name} "{value}"' in conf, (
            f"{name} does not match what nginx sends, so the two paths have diverged"
        )


def test_the_running_app_mounts_the_spa_when_dist_exists():
    """The CALL, not the callee. These tests drive `_mount_spa` on a fresh FastAPI app.
    The native install is `app` in this module, gated on `web/dist` existing.
    `if False and _DIST.is_dir()` left 15 passed; unwiring it restores a process
    that never serves the SPA.
    """
    import ast
    import inspect

    import app.main as main_mod

    tree = ast.parse(inspect.getsource(main_mod))
    found = False
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Call)
            and isinstance(test.func, ast.Attribute)
            and test.func.attr == "is_dir"
        ):
            continue
        for stmt in ast.walk(node):
            if not isinstance(stmt, ast.Call):
                continue
            fn = stmt.func
            if isinstance(fn, ast.Name) and fn.id == "_mount_spa":
                found = True
    assert found, (
        "production app never calls _mount_spa when web/dist exists — tests drive "
        "the helper on a fresh FastAPI app, so a native install would serve no SPA"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/%2e%2e/secret.env",
        "/%2E%2E/secret.env",
        "/..%2fsecret.env",
    ],
)
def test_traversal_outside_bundle_returns_404_not_bytes(client, bundle, path):
    """nginx `root` confines reads to the bundle. Without containment, `dist / '../.env'`
    resolves to a sibling file and the catch-all serves it (GRPH-577)."""
    (bundle.parent / "secret.env").write_text("LEAKED")
    resp = client.get(path)
    assert resp.status_code == 404
    assert b"LEAKED" not in resp.content


def test_hashed_assets_cache_immutably(client):
    """Match `web/nginx.conf.template` `location /assets/` — stale index pinning old JS
    is the failure mode for the shell; hashed assets must cache hard."""
    resp = client.get("/assets/index-abc123.js")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == SPA_ASSET_CACHE_CONTROL


def test_shell_and_version_txt_revalidate(client):
    """Match nginx `location /` — index.html names the hashed bundle and must revalidate."""
    assert client.get("/").headers["Cache-Control"] == SPA_SHELL_CACHE_CONTROL
    assert client.get("/version.txt").headers["Cache-Control"] == SPA_SHELL_CACHE_CONTROL
    assert client.get("/items/GRPH-1").headers["Cache-Control"] == SPA_SHELL_CACHE_CONTROL
