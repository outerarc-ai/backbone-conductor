# Backbone Conductor: Git-native coordination for coding work

[![CI](https://github.com/outerarc-ai/backbone-conductor/actions/workflows/ci.yml/badge.svg)](https://github.com/outerarc-ai/backbone-conductor/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/backbone-conductor.svg?cacheSeconds=3600)](https://pypi.org/project/backbone-conductor/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://pypi.org/project/backbone-conductor/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[简体中文](README.zh-CN.md) · [Documentation](docs/README.md) · [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

Backbone Conductor keeps intentions, decisions, assignments, and review evidence together in Git. Teams can agree on the work before implementation, compare submitted changes with the agreed scope, and record approval against the code that was actually merged. The core runs without a model provider.

## Why Backbone Conductor

- **Shared intent.** Record the purpose, constraints, affected symbols, and expected paths before work begins.
- **Decision lineage.** Link decisions to intentions and inspect superseded or reverted decisions in a browser map.
- **Verified delivery.** Tie each task to a specific Git commit, check its declared scope, and inspect the integrated target branch.
- **Auditable review.** Record who approved a task, which ledger version they observed, and which target commit they reviewed.
- **Flexible interfaces.** Use the CLI, HTTP API, MCP tools, or the repository's Codex plugin. A read-only lifecycle map connects intentions to tasks, artifacts, and approvals.

Deterministic checks identify declared conflicts and Git-path overlap; they do not establish semantic correctness. Optional DeepSeek Harness advice is advisory. People remain responsible for decisions, code review, and Git integration.

## Install

Requires Python 3.12+ and Git:

```sh
python -m pip install backbone-conductor==0.1.0
backbone --help
```

To run the examples from source, install [uv](https://docs.astral.sh/uv/) and use:

```sh
git clone https://github.com/outerarc-ai/backbone-conductor.git
cd backbone-conductor
uv sync --locked --group dev
uv run python examples/demo.py
uv run python examples/two_agent_demo.py --mcp
```

The examples create temporary repositories and do not change this checkout. Their participants and approvals are scripted demonstration data.

## Start a project

Run these commands in a Git repository that already has an initial commit:

```sh
backbone --repo /absolute/path/to/project init
backbone --repo /absolute/path/to/project status
backbone --repo /absolute/path/to/project serve
```

The local API is available at `http://127.0.0.1:8000/docs`. Open `/decision-map` to browse decision lineage and `/lifecycle-map` to follow intentions through assignments, Git artifacts, and approvals. Both views are read-only and support search, dragging, and zooming. Authenticated members see work within their role; reviewers and administrators see the complete graph.

The typical workflow is:

1. Propose an intention with scope and constraints; a reviewer accepts it.
2. Assign a task and implement it in a Git branch.
3. Submit the branch and exact commit as an artifact. Backbone checks scope, decisions, and blocking conflicts.
4. Review the complete patch and merge the code into the target branch.
5. Record approval against the observed ledger version and merged target commit.

For Codex, the [plugin guide](docs/CODEX_PLUGIN.md) explains installation and role-bound MCP access. The [operations guide](docs/OPERATIONS.md) covers authenticated remote access, synchronization, and recovery.

## Documentation

| Guide | Contents |
| --- | --- |
| [Overview](docs/README.md) | Documentation index |
| [Implementation](docs/IMPLEMENTATION.md) | Architecture and review boundaries |
| [Operations](docs/OPERATIONS.md) | Deployment, credentials, synchronization, and recovery |
| [MCP API](MCP_API.md) | Tool scopes and transports |
| [Conflict rules](CONFLICT_RULES.md) | Deterministic checks |
| [DSH integration](DSH_INTEGRATION.md) | Optional semantic advice |
| [Validation](docs/VALIDATION.md) | Reproducible verification and evidence limits |
| [Releasing](docs/RELEASING.md) | Package build and publishing process |

Issues and pull requests are welcome. See [Contributing](CONTRIBUTING.md) for setup and review expectations. Backbone Conductor is released under the [MIT License](LICENSE).
