# Releasing `gban` and `gbfleet`

Two packages, two lifecycles, two tags. `graphban-cli` installs on a laptop and pulls
nothing; `graphban-fleet` runs waves and brings httpx. One shared version number would mean a
`gban` release every time an adapter changed.

```bash
git tag cli-v0.2.0   && git push origin cli-v0.2.0     # publishes graphban-cli
git tag fleet-v0.3.1 && git push origin fleet-v0.3.1   # publishes graphban-fleet
```

`.github/workflows/release-cli.yml` and `release-fleet.yml` each build one package, **refuse
when the tag and the pyproject version disagree**, run `twine check`, and upload through PyPI
trusted publishing. Bump the version in `cli/pyproject.toml` or `fleet/pyproject.toml` first —
the tag does not set it, it asserts it.

**Two workflow files, and that is a PyPI constraint before it is a preference.** Warehouse's
`PendingGitHubPublisher` has a unique constraint on `(repository_owner, repository_name,
workflow_filename, environment)` — and **not** on the project name. So one repository
publishing two projects through one `release.yml` and one environment cannot register the
second publisher at all; the tuple is already taken. It is also what PyPI's security model
asks for: *"isolate responsibility to the smallest (and least-privileged) possible separate
workflow"*. The environments are separate too (`pypi-cli`, `pypi-fleet`), so approving one
release is not approving the other.

The refusal is not fussiness. A version number on PyPI **can never be reused**: a wrong one
published once is published forever, and the only remedy is another number. Everything the
workflow does before uploading exists to make that impossible.

## The one-time setup, which cannot be automated from here

Trusted publishing is an act on a PyPI account, so a workflow cannot arrange it. For **each**
project — `graphban-cli` and `graphban-fleet`:

1. **<https://pypi.org/manage/account/publishing/>** — the ACCOUNT-level form, not the
   project one. A project's *Publishing* tab only exists once the project does, and neither
   of these has been uploaded yet; the account form is where a publisher for a project that
   does not exist is called *pending*.
2. For **`graphban-cli`**: owner `asc-me`, repository `graphban`, workflow
   **`release-cli.yml`**, environment **`pypi-cli`**.
3. For **`graphban-fleet`**: the same owner and repository, workflow **`release-fleet.yml`**,
   environment **`pypi-fleet`**.

   The workflow field wants the FILENAME, not the workflow's `name:`. And the two must differ
   in at least one of workflow-or-environment: PyPI's uniqueness constraint ignores the
   project name, so identical tuples are refused with the second project unable to register
   at all. Both differ here, deliberately.
4. In this repository, Settings → Environments → New environment, twice: `pypi-cli` and
   `pypi-fleet`. Add yourself as a required reviewer on each if you want every publish to be
   a deliberate click.

PyPI requires two-factor authentication on any account that uploads. If yours does not have
it, that is the first blocker rather than anything here.

Until step 1 exists for a project, its first tag fails at the upload with an authentication
error — and nothing has been published, which is the safe direction to fail in.

**"Pending" is the correct state and stays that way until the first successful upload.** It
is not a step that is still running or a configuration that has not taken: PyPI calls a
publisher pending while the project it names does not exist, and converts it to an ordinary
publisher the moment a release lands. Nothing has gone wrong if it reads pending for weeks.

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
