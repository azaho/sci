# Releasing pysci

The repository builds one `pysci` distribution containing the Python library
and the `sci` command. Package releases are independent of the SCI protocol
version. The first unified package release is `0.2.0` (protocol draft `0.1`).

## One-time PyPI setup

First create the GitHub environment `pypi` in this repository's **Settings →
Environments**. Add the owner as a required reviewer and restrict deployment
branches/tags to tags matching `pysci-v*`. Do this before the first release:
an environment created implicitly by a workflow has no review protection.
The owner must review the release commit and approve the publishing job.

These instructions target the new SCI implementation at `azaho/sci`.
The legacy implementation is maintained separately at `azaho/sci-legacy`.
Configure the PyPI publisher for the final repository name, `sci`.

Sign in to the PyPI account that should own `pysci`. On
[account publishing](https://pypi.org/manage/account/publishing/), add a
pending GitHub publisher with these exact values:

| Field | Value |
| --- | --- |
| PyPI project name | `pysci` |
| Owner | `azaho` |
| Repository name | `sci` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

The workflow name is the filename, not its display name or full path.
The workflow declares the GitHub environment `pypi`; it must match the
publisher's environment name. No PyPI API token or GitHub secret is needed.

A pending publisher creates the project on its first successful upload;
it does not reserve the name in advance. If `pysci` already exists under
your account, add the same publisher in that project's publishing settings
instead. See [PyPI's setup instructions](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

## Rehearse

After the workflow is merged, run **Publish pysci** manually from GitHub
Actions, or:

```sh
gh workflow run publish.yml --repo azaho/sci --ref main
```

A manual run builds, tests, and uploads the wheel and source archive as
GitHub Actions artifacts. It does not publish to PyPI. Changes to the
workflow receive the same rehearsal on pull requests.

## Publish a version

1. Update `python/pysci/__init__.py` when releasing a new package version.
   Merge the change and wait for the repository checks to pass. The initial
   `0.2.0` version is already set.
2. Publish a GitHub release tagged `pysci-v<VERSION>` from `main`. For the
   first release:

   ```sh
   gh release create pysci-v0.2.0 --repo azaho/sci --target main \
     --title "pysci 0.2.0" --generate-notes
   ```

3. Watch the **Publish pysci** workflow. It checks that the tag matches the
   package version and that its commit belongs to `main`, runs Python checks,
   builds the distributions, and tests clean uv and pip installations.
   Only the separate publishing job requests the OIDC credential and uploads
   those tested artifacts to PyPI.
4. Verify the registry installation from a clean environment:

   ```sh
   uvx --from pysci==0.2.0 sci --version
   ```

Publishing a GitHub release triggers the upload; creating a draft release
or merely merging code does not. Other release tag prefixes are ignored.
PyPI release files cannot be replaced: changes after a successful release
need a new package version.

## Update Sciscape

After the registry installation succeeds, update the Sciscape homepage to
show `uv tool install pysci` (and `pip install pysci` for project environments),
use repository addresses in the `--archive` examples, and deploy the
Sciscape web Worker through that repository's deployment process.

The Python package does not require a Sciscape database migration or API change.
Update this repository's installation docs to remove their pending-release
wording once the first PyPI release is available.

## Switching a legacy installation

The [legacy `scipub` package](https://github.com/azaho/sci-legacy) also installs
a command named `sci` and uses `~/.sci/` by default. Before switching, securely
back up that directory and check which environment supplies the current command.
Keep legacy and new SCI in separate environments with separate `SCI_HOME`
directories if using both; do not assume their configuration or key formats
are interchangeable.
