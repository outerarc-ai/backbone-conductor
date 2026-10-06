# Repository guidance

The implementation, tests, and published documentation in this repository define Backbone Conductor's behavior. Keep changes scoped to this project.

## Implementation and verification

- Python 3.12+, src layout, uv.lock. Install with `uv sync --locked --group dev`.
- Core models and conflict rules do not depend on any agent runtime.
- All state mutations go through Conductor and GitStore; do not write `.backbone` directly.
- Keep Git audit commits separate from unrelated staged source changes.
- Do not claim semantic checks passed when no model/human review occurred.
- DSH review is advisory; task completion requires human approval and actual integration into the assigned branch.
- Validate with `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest --cov=backbone_conductor --cov-fail-under=85`, `uv run python scripts/evaluate.py` and `uv run python examples/demo.py`.
- If an environment hides editable `.pth` files, source tests can run with `PYTHONPATH=src .venv/bin/pytest`; verify the built wheel separately.

See docs/IMPLEMENTATION.md for the current architecture and validation boundaries.
