# Codex plugin

The repository includes a Codex plugin at `plugins/backbone-conductor`. It combines a role-scoped Backbone MCP server with the `manage-intent` workflow skill. The plugin uses the Git ledger as its source of truth and does not grant approval authority to a model.

## Install

Install Backbone Conductor with Python 3.12+ and ensure `backbone` is executable. A source checkout with `uv sync --locked --group dev` provides `.venv/bin/backbone`. Register the repository catalog at `.agents/plugins/marketplace.json` with Codex:

```sh
codex plugin marketplace add /absolute/path/to/backbone-conductor
codex plugin add backbone-conductor@outerarc-ai
```

Open a **new** Codex task after installation or after updating the plugin; running tasks do not reload bundled skills and MCP configuration. Local Codex clients load the plugin on the same host where the `backbone` executable and target repository are available.

For a local member session, provide these environment variables to the Codex host before starting it:

```sh
export BACKBONE_REPO=/absolute/path/to/coordinator-checkout
export BACKBONE_MEMBER=alice
export BACKBONE_CODEX_ROLE=member
export BACKBONE_EXECUTABLE=/absolute/path/to/backbone
codex
```

`BACKBONE_EXECUTABLE` is optional when `backbone` is on `PATH`. When it points to this repository's `.venv/bin/backbone`, the launcher adds the adjacent `src` directory to the subprocess import path; this also works in sandboxes that hide editable `.pth` files. `BACKBONE_REPO` must be an existing absolute path. Add `BACKBONE_LEDGER_BRANCH=backbone` only when the coordinator checkout has an attached separate metadata branch. For a local coordinator session, set `BACKBONE_CODEX_ROLE=coordinator` and omit `BACKBONE_MEMBER`; its MCP tools can draft and dispatch accepted work but cannot accept proposals, arbitrate, or approve a merge. Check the connected `backbone` tools before relying on them. The launcher rejects missing or unsupported role configuration rather than opening the unbound administrator MCP scope.

Example prompts:

- “Use Backbone to show my assigned task and the decisions I must follow.”
- “Before editing, create a draft intention for this change and show me what the reviewer needs to decide.”
- “Check for new decisions, then prepare my exact Git artifact for submission.”

The coordinator's browser views complement these tools: `/decision-map` shows decision lineage, while `/lifecycle-map` shows intentions, assignments, submitted Git artifacts, and merge approvals as a linked read-only graph. With HTTP authentication, a member token opens a projection of that member's authored intentions, assigned tasks, and related decisions; reviewer and admin tokens see the global graphs. Neither browser view grants write or approval authority.

The local member binding is a tool-scope constraint inside one operating-system account. It is **not** evidence that different people worked independently.

## Work with remote participants

The coordinator operates the authenticated HTTPS service from a separate checkout. Each member and reviewer uses their own token file and repository clone. Members use the plugin's authenticated remote MCP bridge; reviewers use `backbone reviewer` against the remote service. Neither role needs filesystem access to the coordinator checkout. The skill's [remote setup reference](../plugins/backbone-conductor/skills/manage-intent/references/remote-setup.md) gives the role-specific steps. For a multi-person evaluation, use the [team evaluation guide](TEAM_EVALUATION.md).

For a remote member session, provide these environment variables to the member's Codex host before starting it. The coordinator must expose `/mcp` through trusted HTTPS. The bridge reads the owner-only token file at startup and for each tool call, verifies the expected member and exact eight-tool scope, and never puts the token in plugin files:

```sh
export BACKBONE_MCP_URL=https://coordinator.example.org:8443/mcp
export BACKBONE_MEMBER=alice
export BACKBONE_TOKEN_FILE=/private/path/alice.token
export BACKBONE_CODEX_ROLE=member
codex
```

For a private CA, set `BACKBONE_MCP_CA_FILE` to its certificate path. The token file must be owned by the member, be a single-link regular file, and have mode 0600. The remote bridge requires the `backbone` executable locally but never opens the coordinator repository. It accepts plain HTTP only for loopback tests.

Do not set `BACKBONE_REPO` or `BACKBONE_LEDGER_BRANCH` in a remote member session. The launcher rejects mixed local and remote configuration.

Reviewers use the authenticated CLI inside Codex. They can disable the bundled member MCP server while keeping the plugin skill enabled:

```toml
[plugins."backbone-conductor@outerarc-ai".mcp_servers.backbone]
enabled = false
```

Reviewers record their own verdict after inspecting the complete fixed patch and the integrated target. A model response, token name, or scripted test does not count as a human review. Keep private evidence and feedback outside the repository.

The plugin is distributed through this repository's Codex marketplace catalog. The Python package is distributed separately through PyPI.
