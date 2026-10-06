# MCP API 与 Agent 接入

启动 `backbone --repo /absolute/repo mcp --member alice`，使用官方 MCP SDK 的 stdio 传输。stdout 仅用于协议。

| 工具 | 参数 | 返回 |
| --- | --- | --- |
| get_my_task | member_id? | 任务、意图、约束、决策、冲突 |
| create_intent | intent_data | draft 意图 |
| log_decision | decision_data | proposed 决策 |
| start_task | task_id, member_id? | in_progress 任务 |
| fetch_artifact_branch | task_id, branch, expected_sha, member_id?, remote? | 从已配置 Git 远端获取指定提交的成员代码分支 |
| rebase_task | task_id, expected_version, member_id? | 刷新目标分支与决策上下文 |
| submit_artifact | artifact, member_id? | Git 证据、检查、冲突 |
| check_backbone_sync | member_id?, since_version? | 当前版本、新增/撤回决策、阻塞冲突 |

绑定成员时可省略 member_id；冒用其他成员或 author 会报错。artifact 至少含 intent_id、branch、summary，base_ref 默认为 main，且必须匹配任务目标分支。

新仓库采用独立元数据分支时，在启动 MCP 服务的 `mcp` 子命令前加全局选项 `--ledger-branch backbone`；`--repo` 仍指向代码仓库。

省略 `--member` 为本地管理员进程，额外暴露 dispatch_task、transition_intent、transition_decision、revert_decision、revise_intent、replace_intent、review_intent、cancel_task、detect_conflicts、resolve_conflict、inspect_task、merge_task、verify_audit_signatures、refresh_backbone、reconcile_backbone。`replace_intent` 接受 intent_id、字段 patch、author、reason 和 expected_version；旧意图须已接受且没有活跃任务。`review_intent` 接受 intent_id、accepted/rejected outcome、reviewer、rationale 和 expected_version；仅能审查他人草稿。`revert_decision` 需要 decision_id、author、rationale 和 expected_version，只能撤回已接受决策并留下审计证据。`verify_audit_signatures` 只检查最近 limit 个元数据提交并返回有效、未签名和无效数量。不得将其当作远程认证服务。

`mcp --coordinator` 是单独的本地受限工具范围，只含 `get_coordination_state`、`create_intent`、`log_decision`、`detect_conflicts`、`dispatch_task` 和 `inspect_task`。创建的草稿意图与建议决策固定归属 `conductor-agent`；分派仍须事先由人接受意图。该范围不含 `review_intent`、`transition_intent`、`transition_decision`、`revert_decision`、`resolve_conflict`、`merge_task` 或远端同步；没有远程 HTTP 协调代理入口。可由 `backbone conductor` 的 DSH 运行器启动，运行前会核对精确工具列表及状态读取。它是本机 OS 账户内的工具限制，不是独立身份认证或进程隔离。

工具 Schema 由 MCP tools/list 提供；领域 Schema 可由 `backbone schema` 或 HTTP `/schema` 获取。

## Codex

先安装本项目。替换两个绝对路径：

```sh
codex mcp add backbone -- /absolute/backbone-conductor/.venv/bin/backbone \
  --repo /absolute/your-project mcp --member alice
```

或配置 stdio server：

```toml
[mcp_servers.backbone]
command = "/absolute/backbone-conductor/.venv/bin/backbone"
args = ["--repo", "/absolute/your-project", "mcp", "--member", "alice"]
```

配置已对照本机 CLI 和 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) 核实。项目不会自动修改全局客户端设置。其他 stdio MCP 客户端可使用相同 command/args。

Codex 插件将受限成员或协调者 MCP 与意图工作流程打包，安装与使用方法见 [Codex 插件指南](docs/CODEX_PLUGIN.md)。远端成员也可运行 `backbone mcp-remote-member --url https://coordinator.example.org:8443/mcp --member alice --token-file /private/alice.token`，将已认证的 HTTPS 成员 MCP 桥接为本机 stdio；启动前核对成员身份与精确八工具范围，每次调用从私有文件重新读取令牌。插件启动器使用环境变量配置同一桥接，不需要成员本地持有协调仓库。

可选 DeepSeek Harness 成员代理可直接通过 `backbone dsh --member ... --workspace ... --dsh-home ... --model ... --prompt ...` 连接同一成员绑定服务；完整命令及运行边界见 [DSH_INTEGRATION.md](DSH_INTEGRATION.md)。

## 远程成员接入

`serve --auth-file /private/tokens.json --mcp-http` 在同一服务的 `/mcp` 开启无状态 Streamable HTTP。它只暴露上表八个成员工具；每个请求都用现有私有凭据文件核对 bearer 令牌，把 member_id 和 author 绑定到令牌 principal。管理员与审查者令牌不能进入该 MCP 端点，成员不能通过请求参数冒用其他成员。令牌原子轮换后立即生效，凭据文件损坏或权限不安全时端点返回 503。工具调用产生的 Git 审计提交记录已认证的 HTTP principal/role。

远程客户端连接 `https://coordinator.example.org:8443/mcp`，发送 `Authorization: Bearer <成员令牌>`；启动时增加 `--mcp-allowed-host coordinator.example.org:8443`，使传输层接受该实际 Host header。该参数接受 Host 值，不接受 URL 或通配符。默认只接受本机回环地址。部署时应使用可信 HTTPS 或可信代理并隔离协调仓库的 OS 写权限；静态 bearer 令牌不是 OAuth 授权服务器，也不能证明令牌背后的自然人身份。此入口不主动向 Agent 会话推送任务，仍由客户端调用 `get_my_task` 拉取。

没有可配置静态 bearer 的 MCP 客户端时，可用 `backbone member --url https://coordinator.example.org:8443 --token-file /private/path/alice.token tasks` 通过同一认证服务读取任务；后续 `start`、`updates`、`rebase`、`fetch`、`submit` 及提案命令见[远程成员 CLI](docs/OPERATIONS.md#远程成员-cli)。该 CLI 是人可直接使用的 HTTP 客户端，不改变 MCP 的工具范围或权限模型。

ASGI 挂载与会话管理遵循 [官方 MCP Python SDK 的部署说明](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/asgi.md)；当前仅验证锁定的 MCP 1.x 版本。

## 成员流程

1. 获取任务，阅读约束与决策，调用 start_task。
2. 在独立代码分支/worktree 实现，期间调用 check_backbone_sync。
3. 提交并推送代码，调用 fetch_artifact_branch 传入功能分支和完整提交 SHA，再以返回的 tracking_ref 和相同 commit_sha 调用 submit_artifact。
4. 阻塞冲突交给管理员裁决；通过后仍需人工审查和真正的 Git 合并。

协调端保持在目标分支。get_my_task 是拉取接口；首版不主动向 Agent 会话推送消息。
当 check_backbone_sync 返回 new_decisions 或 withdrawn_decisions，成员应执行 rebase_task，使用当前 version 作为 expected_version，再提交制品。提交后才发生的决策变化由管理员在 merge_task 的 rationale 中明确审查并记录。
代码实际合入目标分支后，管理员须重新调用 `inspect_task`，从结果取 `version` 和 `git.target_sha`，作为 `merge_task` 的 `expected_version` 与 `expected_target_sha`；若审查后账本或目标分支变化，调用被拒绝并须重新审查。完成后 `inspect_task` 可用审批时保存的账本版本和目标 SHA 重读当时的审查包，并单独报告当前版本与当前目标 SHA；旧完成任务若没有结构化锚点，则明确拒绝伪装历史审查。
