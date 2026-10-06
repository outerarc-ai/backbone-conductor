# DSH 集成

通过可选的 `dsh` extra 安装 DeepSeek Harness SDK。`uv.lock` 固定依赖版本；模型建议不会改变 Backbone 的确定性检查或审批权限。

DSHReviewer 将任务、意图、已接受决策和真实 diff 作为明确标记为不可信数据的 JSON 随请求提供，在一次性目录内以明确指定的 home 启动 DeepSeekHarness 的 sdk-minimal profile。运行时补丁禁用该 profile 默认的持久 bash/PowerShell 工具，并把文件策略设为 `read-only`；模型审查不需要本地命令或读取仓库。输出必须符合 SemanticReview Schema；非法输出、超时和未完成回合不会生成批准。调用结束关闭 runtime，再核对 Backbone 版本和制品 SHA，拒绝过期结果。成功审查的 `runtime` 字段记录从创建 SDK 客户端到关闭的 `elapsed_ms`、SDK 返回的 `session_id` 与 `finish_reason`，随建议一起进入 Git 审计。失败尝试不修改 Backbone 状态；这些指标不代表模型生成阶段的独立耗时。

审查入口同样要求 DSH home 不为符号链接、由当前用户持有且权限为 0700，以保护其中的会话日志。

可选 `--attempt-log` 记录通过任务和 diff 前置校验后的审查尝试结果。路径必须是仓库和 Git 目录外的绝对路径，父目录仅当前用户可访问（0700），日志文件为仅当前用户可读写的普通文件（0600）；命令会在调用模型前验证该位置。JSONL 事件包含任务 ID、模型/provider 名称、审阅前版本、制品 SHA、结果、阶段和总耗时；失败时另记错误**类型**。不包含提示词、代码 diff、令牌或 provider 原始错误内容。成功审查先写入 Git 审计，随后才记录 `committed` 事件；若这一步日志写入失败，命令会提示审查已提交，应先核对状态再重试。此文件是私有运行指标，不是 Backbone 审计提交，也不保证捕获进程崩溃或前置校验失败。

```sh
uv sync --locked --extra dsh
mkdir -m 700 /absolute/private-review-metrics
uv run backbone --repo /absolute/project review TASK_ID \
  --dsh-home /absolute/isolated-dsh-home --model YOUR_MODEL \
  --attempt-log /absolute/private-review-metrics/attempts.jsonl
uv run backbone --repo /absolute/project review-stats \
  --attempt-log /absolute/private-review-metrics/attempts.jsonl
```

`review-stats` 只读汇总该私有日志中的已记录尝试数、已提交数、失败阶段、记录内提交比例和总耗时中位数；这是日志样本的统计，不代表所有请求的真实成功率。旧版仅记录失败的日志会标出 `legacy_failure_records`，其提交比例返回 `null`。token 用量与费用返回 `null`，因为锁定 SDK 的 `RunResult` 没有稳定字段。指标不包含独立模型生成耗时，也不替代 Git 审计中的审查结果。

需在本地配置 provider 凭据，不写入 Git。审查会向该 provider 发送任务和代码 diff；结果只作建议。补丁禁用默认 shell，但指定的 DSH home 若有自定义补丁，仍可能装载其他工具；请使用专用 home 和适当的运行账户。一次性目录与只读工具策略不限制 Harness 进程自身的 OS 权限或向 provider 发送数据。

## 意图配对语义建议

`backbone conflict advise INTENT_A INTENT_B --dsh-home /absolute/private-home --model YOUR_MODEL` 在实施前比较两份非终态意图。输入仅包含两份意图的计划字段、相关已接受决策、当前确定性冲突证据与观察到的账本版本，限 1 MB；不传代码 diff。返回 Schema 限定的 `conflict / compatible / uncertain` 建议、理由、证据和协调建议，并附输入 SHA-256、运行元数据与是否因并发账本变化而过期的标记。该入口只读，不会把模型建议写成 Backbone 冲突、解除阻塞或批准合并。调用会将这些协调数据发送给配置的 provider。此接口不提供经独立标注的语义准确率保证。

## 受限协调代理

`backbone conductor` 在本地仓库启动 DSH `sdk-minimal`，以一次性只读工作目录和私有补丁连接 `backbone mcp --coordinator`。该 MCP 服务**只**公开读取完整协调状态、创建 `conductor-agent` 作者的草稿意图、提出同作者的建议决策、检测确定性冲突、分派已接受意图、读取固定提交审查包六项工具。意图接受、决策接受、冲突仲裁、任务合并审批及远端同步均不在工具列表中；运行前独立 MCP 握手要求精确工具集合并读取状态，若权限扩大则拒绝启动模型。DSH 补丁禁用默认持久 shell，并将文件策略设为只读。模型不能通过这些工具完成需要人类审查的状态转换。

```sh
uv sync --locked --extra dsh
uv run backbone --repo /absolute/project conductor \
  --dsh-home /absolute/private-coordinator-home --model YOUR_MODEL \
  --prompt "读取当前状态，提出待人审查的计划并分派已接受工作"
```

可用 `--ledger-branch backbone` 指定独立元数据分支，可用 `--session-id` 指定该仓库命名空间内的会话 ID，并用 `--prompts-file` 提供 JSON 字符串数组，在单次运行中连续执行多回合；锁定 SDK 在进程结束后不能恢复已持久化的同 ID 会话。DSH home 必须在仓库外，由当前用户持有且仅当前用户可访问（0700）；会话日志在该 home 中。调用模型时，协调状态和由 `inspect_task` 读取的代码审查包可能发送给所配置的 provider。MCP 子进程仍以运行者的 OS 权限访问本地仓库，DSH 工具策略不是 OS 隔离；专用 home 若装有额外插件，还需单独审查其能力。请以专用运行账户和合适的 Git 文件权限运行。模型输出的质量取决于所配置的 provider，应由使用者核对。

## 成员代理接入

`backbone dsh` 使用同一可选 SDK 的 `sdk-minimal` profile，在临时补丁中装载 `@deepseek-ai/dsh-mcp-client`，启动绑定 `--member` 的 Backbone stdio MCP 服务。调用模型前，Python 端另起一次 MCP 连接，核对初始化、工具列表、成员上下文，并拒绝管理员工具泄露。`--workspace` 必须是与协调仓库分开的现存目录，`--dsh-home` 必须位于两者之外；补丁把 DSH 工具写入策略设为 `workspace-write`，并在系统提示中要求通过 MCP 操作协调元数据。独立元数据分支可在 `dsh` 子命令前传入 `--ledger-branch backbone`。

```sh
uv sync --locked --extra dsh
uv run backbone --repo /absolute/project dsh --member alice \
  --workspace /absolute/separate-worktree \
  --dsh-home /absolute/isolated-dsh-home --model YOUR_MODEL \
  --prompt-file /absolute/task-prompt.txt
```

多回合时，`--prompts-file` 指向形如 `["读取我的任务", "检查刚才提出的方案"]` 的 JSON 文件，替代 `--prompt` 或 `--prompt-file`。返回值列出每一回合的完成状态、最终文本和耗时；同一会话中的后一回合可以看到前一回合的模型消息与 MCP 工具结果。也可使用 `--interactive` 按行输入新提示，每轮结果立即作为一行 JSON 写到 stdout；空行跳过，`:quit`、`:exit` 或 EOF 结束。交互会话仍使用同一 SDK 进程，退出后不能恢复旧会话。协调代理和远程成员同样支持这两种多回合方式。

本地成员入口会创建或核对 DSH home：拒绝符号链接，要求它位于协调仓库和代码工作区之外、由当前用户持有且权限为 0700。此入口会实际向配置的 provider 发起模型请求；必须由操作者自行配置凭据。返回的会话 ID 带仓库与成员命名空间，可用 `--session-id` 指定该成员命名空间内的会话 ID；其他命名空间的 ID 会被拒绝。`--prompts-file` 接受 JSON 字符串数组，让本地或远程成员在同一 SDK 进程中连续执行多个回合并继承上下文。锁定 SDK 0.1.5rc1 在进程重启后对已持久化的同 ID 会话返回 `already exists`；Backbone 会明确提示此限制，保留旧日志，不会悄悄创建一个伪续接会话。DSH 最小 profile 的 shell 与 MCP 子进程仍以调用者的 OS 身份运行；`workspace-write` 限制模型工具的写入范围，但不构成完整的读取或网络隔离。MCP `--member` 是本地工具约束，不是不同自然人之间的认证。会话日志保存在指定的 DSH home；不要把凭据或敏感日志放入 Git。模型建议、工具调用和代码修改都不能替代人工复核及真实 Git 合并。

远程成员入口改用已启用的 `/mcp` HTTPS 服务，无需本地协调仓库。`--mcp-url` 和 `--mcp-token-file` 必须同时指定；URL 只能是 HTTPS `/mcp`，回环测试地址可用 HTTP。令牌文件须由当前用户持有、权限为 0600、硬链接数为 1，且位于工作树和专用 DSH home 外；DSH home 须为当前用户持有的 0700 目录。可用 `--mcp-ca-file` 信任自签证书。正式 DSH MCP 客户端插件采用 `streamable-http` transport 与 Authorization header；调用模型前，独立的 Python MCP 会话以同一令牌验证实际成员身份与精确工具范围。一次性补丁为 0600，含本回合明文 bearer header，用完删除；指定的 DSH home、进程权限和运行日志仍须按敏感数据管理。会话 ID 按远程 URL 与成员命名空间绑定，令牌轮换后需更新私有文件并重启回合。

```sh
uv run backbone dsh --member alice \
  --workspace /absolute/separate-worktree --dsh-home /absolute/private-dsh-home \
  --mcp-url https://coordinator.example/mcp \
  --mcp-token-file /absolute/private/alice.token \
  --model YOUR_MODEL --prompt-file /absolute/task-prompt.txt
```

自动化检查覆盖 SDK 启动、MCP 工具范围、模拟 provider 调用和 Git 审计归属。这些检查不提供真实模型质量、费用或公网部署性能的保证。SDK `RunResult` 没有稳定的 token 用量与费用字段，因此 `review-stats` 不推算费用。

依据：[DSH 官方 Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)、[SDK 入门](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/python-sdk.md)、[DSH MCP 客户端](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)及[跨进程会话恢复问题](https://github.com/deepseek-ai/deepseek-harness/discussions/6295)。MCP 服务端使用 [官方 MCP Python SDK v1](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)，固定 `<2` 避免主版本 API 变化。
