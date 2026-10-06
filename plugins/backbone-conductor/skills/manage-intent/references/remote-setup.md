# Remote team access in Codex

Each participant opens a separate Codex task on their own machine with their assigned repository and private token. The coordinator runs the authenticated HTTPS service from a separate coordination checkout. Members configure `BACKBONE_MCP_URL`, `BACKBONE_MEMBER`, and `BACKBONE_TOKEN_FILE` for the plugin's stdio bridge; reviewers configure `BACKBONE_URL` and `BACKBONE_TOKEN_FILE` for the authenticated CLI. Never paste token contents into a prompt or shared transcript. The [team evaluation guide](../../../../../docs/TEAM_EVALUATION.md) defines a distinct-person assessment procedure.

## Member session

The plugin checks the remote member identity and exact eight-tool scope before exposing tools. Call `get_my_task` to read the assignment, `start_task` after checking its accepted intent, and `check_backbone_sync` as work proceeds. Use `create_intent` or `log_decision` only for proposals. If decisions changed, call `rebase_task` with the observed ledger version before submission. If the plugin cannot connect, run the read-only `backbone member-check --mcp-url "$BACKBONE_MCP_URL" --member "$BACKBONE_MEMBER" --token-file "$BACKBONE_TOKEN_FILE"` to diagnose endpoint, identity, and tool scope; add `--ca-file "$BACKBONE_MCP_CA_FILE"` when using a private CA.

Commit and push the code in the member's own clone. Read the full SHA with `git rev-parse HEAD`. Call `fetch_artifact_branch` with task ID, pushed branch and full SHA, then `submit_artifact` with the returned tracking ref and same SHA. The remote member MCP bridge does not need the coordinator checkout.

## Reviewer session

Run `backbone reviewer --url "$BACKBONE_URL" --token-file "$BACKBONE_TOKEN_FILE" whoami` and verify role `reviewer`. Read `state`, `intents`, `decisions`, `conflicts`, and `tasks` as needed. For an intention, review the actual context and use `review-intent INTENT_ID --outcome accepted|rejected --rationale TEXT --version OBSERVED_VERSION` only to record the human reviewer's verdict.

For submitted code, run `inspect TASK_ID --full`. Inspect both the fixed artifact patch and, after the coordinator performs the Git merge, the integrated target diff. The reviewer may use Codex to analyze the patch, but must make the verdict personally. On approval, use the fresh inspection's `version` and `git.target_sha` with `approve TASK_ID --rationale TEXT --version VERSION --target-sha SHA`. A rejected or stale response requires a fresh inspection, never reused values. Use `audit-snapshot` and `audit-history --all` for the trial's recovery checks.

## Team record

Save a private record of participant roles, separate devices/clones, intention/task IDs, pre-work and submitted SHAs, merge SHA, reviewed ledger version and target SHA, failed steps, elapsed time, and feedback. Record whether each action was completed by a distinct person. Do not represent scripted identities, CLI role strings, or model output as proof of human participation.
