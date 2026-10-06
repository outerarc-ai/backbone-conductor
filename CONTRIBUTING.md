# Contributing to Backbone Conductor

Thanks for helping improve Backbone Conductor. Please open an issue before substantial interface or storage changes so their compatibility and audit implications can be discussed.

## Development setup

Use Python 3.12 or newer, Git, and uv:

```sh
git clone https://github.com/outerarc-ai/backbone-conductor.git
cd backbone-conductor
uv sync --locked --group dev
```

Run the project checks before opening a pull request:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=backbone_conductor --cov-fail-under=85
uv run python scripts/evaluate.py
uv run python examples/demo.py
```

CI also runs Python 3.12/3.13, real HTTP and MCP integration tests, Compose deployment checks, example workflows, and package builds. If the local environment hides editable `.pth` files, run source tests with `PYTHONPATH=src .venv/bin/pytest` and verify the built wheel separately.

## Pull requests

- Keep protocol models and conflict rules independent of any agent runtime.
- Route ledger changes through Conductor and GitStore; do not edit `.backbone/` files directly.
- Keep metadata audit commits separate from unrelated staged source changes.
- Include tests for behavior or compatibility changes, and update the relevant guide when a public command, interface, or boundary changes.
- Describe user-visible behavior, validation performed, and any remaining limitation. Distinguish scripted checks from human review.

The [implementation guide](docs/IMPLEMENTATION.md) explains the current architecture, and [docs/README.md](docs/README.md) indexes the rest of the documentation. The project uses the [MIT License](LICENSE).
