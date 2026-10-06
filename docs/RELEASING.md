# Release process

Backbone Conductor uses semantic versions. A GitHub release and a PyPI distribution are created from the same tagged commit.

## Publisher setup

1. In the GitHub repository, use the `pypi` Environment with required reviewers and a deployment rule limited to `v*` tags.
2. In PyPI, configure a Trusted Publisher for package `backbone-conductor` with owner `outerarc-ai`, repository `backbone-conductor`, workflow `release.yml`, and environment `pypi`. If the PyPI project does not yet exist, create a pending publisher. No long-lived PyPI token belongs in GitHub secrets.
3. Check that the GitHub account approving the deployment and the PyPI project owner are the intended maintainers. Review any older Trusted Publisher entries and remove identities that should no longer publish.

The [PyPI Trusted Publishing guide](https://docs.pypi.org/trusted-publishers/) explains the owner, repository, workflow, and environment identity checks.

## Publish a version

1. Set the same version in `pyproject.toml` and `src/backbone_conductor/__init__.py`, update `uv.lock`, `CHANGELOG.md`, and the README installation command.
2. Run CI and the local checks from a clean checkout:

   ```sh
   uv sync --locked --group dev
   uv run ruff check .
   uv run ruff format --check .
   uv run pytest --cov=backbone_conductor --cov-fail-under=85
   uv run python scripts/evaluate.py
   uv run python examples/demo.py
   uv build --out-dir /tmp/backbone-release
   uv run python scripts/check_release.py --dist /tmp/backbone-release
   ```

3. Run the GitHub `Release` workflow from the intended commit with `dry_run=true`. Download and inspect the wheel and source archive.
4. Create and push an annotated `vX.Y.Z` tag on that exact commit. Run the `Release` workflow from the tag with `dry_run=false`. Its build job checks the tag and artifacts; the publishing job waits for approval in the `pypi` Environment before using OIDC to upload.
5. Create a GitHub Release from the same tag, using the corresponding `CHANGELOG.md` entry. Verify the PyPI page and installation in a fresh environment with `python -m pip install backbone-conductor==X.Y.Z` and `backbone --help`.

PyPI versions are immutable. Do not reuse a published version or move a published tag. The release workflow grants `id-token: write` only to the publishing job and rejects upload requests started from a branch.

Check `requires-python` and the tested Python version classifiers in `pyproject.toml` before tagging. The specifier controls installer compatibility; classifiers describe the versions shown in package listings and version badges. PyPI keeps the metadata uploaded with a release, so a metadata correction requires a new version. GitHub README badges may briefly show cached data after a new upload; verify the [PyPI project page](https://pypi.org/project/backbone-conductor/) and a fresh installation before treating a badge as release evidence.
