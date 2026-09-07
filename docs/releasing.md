# Releasing `gban` and `gbfleet`

Two packages, two lifecycles, two tags. `graphban-cli` installs on a laptop and pulls
nothing; `graphban-fleet` runs waves and brings httpx. One shared version number would mean a
`gban` release every time an adapter changed.

```bash
git tag cli-v0.2.0   && git push origin cli-v0.2.0     # publishes graphban-cli
git tag fleet-v0.3.1 && git push origin fleet-v0.3.1   # publishes graphban-fleet
```

`.github/workflows/release.yml` builds only the package the tag names, **refuses when the tag
and the pyproject version disagree**, runs `twine check`, and uploads through PyPI trusted
publishing. Bump the version in `cli/pyproject.toml` or `fleet/pyproject.toml` first — the tag
does not set it, it asserts it.

The refusal is not fussiness. A version number on PyPI **can never be reused**: a wrong one
published once is published forever, and the only remedy is another number. Everything the
workflow does before uploading exists to make that impossible.

## The one-time setup, which cannot be automated from here

Trusted publishing is an act on a PyPI account, so a workflow cannot arrange it. For **each**
project — `graphban-cli` and `graphban-fleet`:

1. On PyPI → *Your projects* → *Publishing*, add a **pending publisher** (the project does
   not exist yet; that is what "pending" means).
2. Owner `asc-me`, repository `graphban`, workflow `release.yml`, environment `pypi`.
3. In this repository, create the `pypi` GitHub environment. Adding yourself as a required
   reviewer makes every publish a deliberate click; PyPI matches the environment name too, so
   a workflow that skipped it could not publish even with a publisher configured.

Until step 1 exists for a project, its first tag fails at the upload with an authentication
error — and nothing has been published, which is the safe direction to fail in.

## Then Homebrew

A tap, not homebrew-core: core requires notability this project does not have yet, and a tap
has no such bar. `brew install asc-me/tap/gban` once
[`asc-me/homebrew-tap`](https://github.com/asc-me/homebrew-tap) exists.

`gban` is unusually cheap to package this way. A Homebrew formula for a Python CLI normally
carries one `resource` stanza per transitive dependency, regenerated at every bump —
`graphban-cli` has **none**, so the formula is a few lines that cannot rot. `gbfleet` needs
five (httpx, httpcore, h11, anyio, sniffio + certifi/idna), which `brew update-python-resources`
generates.

Neither formula can be written until the packages are on PyPI: a formula's `url` is a release
artifact, and there is nothing to point at yet. That ordering is the reason this file has the
sections in this order.
