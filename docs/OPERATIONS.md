# 部署与恢复

先初始化代码仓库、配置 Git identity，再运行 `backbone --repo /repo init`。Git identity 为提交身份；本地 CLI/MCP 的领域 author 由调用方声明。启用 HTTP 认证后，HTTP 请求的 author 和 member_id 与令牌 principal 绑定。

HTTP 默认 127.0.0.1:8000，`/docs` 为交互 API、`/health` 检查进程、`/state` 验证快照读取。无认证时拒绝非 loopback 绑定。MCP 默认是客户端管理的 stdio 子进程；带私有令牌文件时可显式启用 `/mcp` 的成员专用 Streamable HTTP。

`/decision-map` 提供只读的决策脉络图。页面与静态资源不包含账本数据；浏览器随后从 `/decision-map/data` 读取同一 Git 账本快照。在启用认证的部署中，该数据端点只允许管理员和审查者，页面中的 bearer 令牌仅保存在当前页面内存，不写入 URL 或本地存储。节点的拖拽位置只保存在该浏览器的 localStorage，重置布局可清除；移动节点不会修改决策、账本或 Git 提交。远程使用仍须按下文配置可信 HTTPS。

## HTTP 令牌

在目标仓库之外创建仅当前用户可读的凭据文件：

```sh
backbone --repo /repo auth create \
  --file /private/path/backbone-http-tokens.json --admin owner \
  --member alice --member bob --reviewer carol
backbone --repo /repo serve \
  --auth-file /private/path/backbone-http-tokens.json
```

创建命令仅输出一次明文令牌，不把它们写入凭据文件；文件权限为 0600，内容是 SHA-256 摘要与 principal/role 映射。创建命令先在同一私有目录的临时文件写入并验证摘要，只在完整校验后以不覆盖的硬链接一次性发布目标路径；发布后再次核对磁盘字节和文件身份。若校验失败，只清理本次创建且仍位于该路径的文件。在 POSIX 系统上，凭据所在目录须由服务运行用户拥有、不可被组或其他用户访问（建议 0700）；非 root 服务还要求文件本身归该用户所有。为支持宿主 UID 挂载到默认 root 容器，root 进程可以读取其他 UID 持有的私有目录与文件。拒绝凭据文件的符号链接与硬链接，创建命令也拒绝悬空符号链接。目录权限、所有权或硬链接数运行期间变得不安全时，受保护请求与健康检查会拒绝继续使用该凭据。妥善保存明文令牌，避免录入 shell 历史、应用日志或 Git。令牌持有者使用 `Authorization: Bearer TOKEN`。`/health` 与不含账本数据的决策图页面及静态资源无需令牌；其他路径均需令牌，未列入相应角色的路径默认被拒绝。管理员可以使用全部端点；成员只能创建自己的草稿意图和提议决策、读取自己的任务和更新、执行自己的任务开始/上下文刷新/产物提交。审查者可读取完整项目快照、意图、决策、任务、已提交及已完成任务的只读代码审查包、冲突、时间线和审计签名报告；可带理由接受或拒绝他人的意图草稿，在实际 Git 合并之后记录任务审批，也可用非空理由裁决冲突，或用观察到的账本版本及理由撤回已接受决策。审查者不能分派、开始或提交任务、执行其他意图/决策状态变更、检测冲突、同步仓库、自我审查或审批分派给自己的任务。该角色只限制 HTTP 操作，不能证明不同令牌由不同自然人持有，也不替代仓库文件权限隔离。

已有凭据文件可针对单个人增删或换令牌，其他人的令牌保持有效：

```sh
backbone --repo /repo auth add --file /private/path/backbone-http-tokens.json \
  --name dave --role member
backbone --repo /repo auth rotate-one --file /private/path/backbone-http-tokens.json \
  --name alice
backbone --repo /repo auth revoke --file /private/path/backbone-http-tokens.json \
  --name bob
```

`add` 和 `rotate-one` 只输出目标 principal 的一次性新明文令牌；`revoke` 只返回姓名与角色，不输出其他人的令牌。重复添加、轮换或撤销不存在的用户会失败，最后一个管理员不得撤销。需要**全员重发**或重置完整名单时，使用 `backbone --repo /repo auth rotate --file /private/path/backbone-http-tokens.json --admin owner --member alice --reviewer carol`；未列出的人会失去访问权。所有变更先验证当前文件，再在私有目录内经跨进程文件锁写入、验证新的 0600 摘要文件并原子替换；该目录会保留不含秘密的 `.lock` 文件。运行中的 HTTP/MCP 服务逐请求读取当前文件，无需重启。若文件缺失、权限不安全或内容损坏，受保护请求和 `/health` 返回 503，不继续接受缓存的旧令牌。直接提供 HTTP 仍是明文传输，远程访问需启用 TLS；若由可信反向代理终止 TLS，后端端口只应接受代理流量。Git 仓库写权限仍需在操作系统层隔离；HTTP 角色不限制拥有仓库文件权限的本机用户。

## 直接 HTTPS（可选）

已有可信证书与对应 PEM 私钥时，可不经反向代理直接运行 HTTPS：

```sh
backbone --repo /repo serve --host 0.0.0.0 --port 8443 \
  --auth-file /private/path/backbone-http-tokens.json \
  --tls-certfile /private/path/server.crt \
  --tls-keyfile /private/path/server.key
```

证书须覆盖客户端连接的主机名并受客户端信任；私钥必须是仓库外、仅所有者可访问的普通文件，不接受符号链接。`--tls-certfile` 与 `--tls-keyfile` 必须一起提供，bearer 认证仍必需。证书续期及服务重启由部署者管理；当前不提供 ACME 自动签发或热加载证书。也可继续使用可信反向代理终止 TLS。此处只验证了本机自签证书下的真实 HTTPS 流程，尚未验证远程服务器或公网部署。

## 成员 Streamable HTTP MCP（可选）

在上述可信 HTTPS 命令中加入 `--mcp-http --mcp-allowed-host coordinator.example.org:8443`，成员 Agent 就可连接 `https://coordinator.example.org:8443/mcp` 并提供自己的 `Authorization: Bearer` 令牌。`--mcp-http` 必须与 `--auth-file` 一起使用；默认 Host 名单只含本机回环地址，远程实际 Host header（含非默认端口）须显式列出。该接口只注册八个成员工具，逐请求认证并绑定成员身份；管理员/审查者令牌返回 403，旧令牌轮换后返回 401。凭据不可用时返回 503；未知 Host 由 MCP 传输层拒绝。已验证本机真实 HTTP 客户端和受信任证书的 HTTPS 客户端，尚未在公网或不同自然人的共享部署中验证。

成员客户端需支持 Streamable HTTP 和静态 bearer header。令牌是长期凭据，应通过客户端的私有配置或环境变量传入，不要把明文放入 Git、公开 URL 或共享日志。连接前可用 `backbone member-check --mcp-url https://coordinator.example.org:8443/mcp --member alice --token-file /private/path/alice.token` 在成员设备上只读验证身份绑定与工具范围，无需模型或协调仓库；实际真人试用的角色、流程和证据要求见[验收清单](TEAM_EVALUATION.md)。此接口未实现 OAuth 动态注册；如果客户端只接受 OAuth 授权发现，需另行提供兼容的身份服务。MCP 的角色权限不能替代协调仓库的操作系统文件权限。

## Docker Compose

为 Compose 创建专用凭据目录，放 `backbone-http-tokens.json` 与凭据管理命令生成的 `.lock` 文件。**挂载目录而非单个文件**，这样宿主执行 `auth add`、`rotate-one`、`revoke` 或全员 `rotate` 的原子替换能被运行中的 Linux 容器看到。

```sh
export BACKBONE_REPO=/absolute/path/to/git-repo
export BACKBONE_AUTH_DIR=/private/path/backbone-auth
mkdir -m 700 "$BACKBONE_AUTH_DIR"
backbone --repo "$BACKBONE_REPO" auth create \
  --file "$BACKBONE_AUTH_DIR/backbone-http-tokens.json" --admin owner
export BACKBONE_UID="$(id -u)"
export BACKBONE_GID="$(id -g)"
docker compose build
docker compose run --rm conductor --repo /workspace init
docker compose up -d
```

目录须预先创建并仅允许受信任用户访问；`auth create` 只输出一次明文令牌。已有凭据文件无需再运行 `auth create`。默认端口为宿主 `127.0.0.1:8000`，可用 `BACKBONE_PORT` 改变宿主端口；容器内强制令牌认证。容器会写入挂载仓库，适合专用协调 checkout；仓库本地 Git identity 优先于镜像默认值。上例让容器进程使用当前宿主用户的 UID/GID；须确保该用户可写仓库、遍历凭据目录并读取私有令牌文件。未设置这两个变量时容器仍以 root 运行。容器不自动配置 Git 远端凭据。

独立元数据分支须在**固定的容器路径 `/workspace`** 内创建或附加，再用 override 启动；不要直接复用宿主创建的隐藏 worktree：

```sh
docker compose -f compose.yaml -f compose.ledger.yaml run --rm \
  conductor --repo /workspace ledger create
docker compose -f compose.yaml -f compose.ledger.yaml up -d
```

若 `backbone` 分支已存在但此容器 checkout 尚无 worktree，改用 `ledger attach`；已有内联快照需先按迁移前置条件运行 `ledger migrate`。独立模式的元数据写入不会移动代码分支 HEAD；容器重启后可从 Git 读取快照。Dockerfile 的 `PYTHON_IMAGE` 构建参数可选择可访问的同等 Python 3.12 基础镜像。

已有证书和私钥时，在相同的仓库、凭据与 UID/GID 配置上叠加 HTTPS。证书目录只放 `server.crt` 和 `server.key`，私钥须由容器 UID 持有且权限为 0600：

```sh
export BACKBONE_TLS_DIR=/private/path/backbone-tls
export BACKBONE_TLS_PORT=8443
docker compose -f compose.yaml -f compose.tls.yaml up -d
```

独立元数据分支使用 `docker compose -f compose.yaml -f compose.tls.yaml -f compose.ledger-tls.yaml up -d`；首次创建分支仍按上文的 `ledger create` 步骤执行。TLS override 默认也只绑定宿主 loopback；确需让远程客户端连接时显式设置 `BACKBONE_TLS_BIND=0.0.0.0`，并配置主机防火墙。基础配置的 `BACKBONE_PORT` 回环映射仍保留，但在 TLS 模式下它同样提供 **HTTPS**，不提供明文 HTTP；`BACKBONE_TLS_PORT` 是额外的 HTTPS 映射。远程部署前应验证证书信任、bearer 认证、审计写入、重启恢复及明文拒绝。

成员需要通过 Compose 连接 MCP 时，先在新凭据文件中列出成员（`auth create --member alice`），已有文件则运行 `auth add --name alice --role member`。再把 `compose.mcp.yaml` 放在所选组合的**最后一个** `-f` 参数。它只设置 `BACKBONE_MCP_HTTP=1`，不覆盖 HTTP/HTTPS 或内联/独立分支的启动命令。例如 HTTPS 独立分支：

```sh
export BACKBONE_MCP_ALLOWED_HOSTS=coordinator.example.org:8443
docker compose -f compose.yaml -f compose.tls.yaml \
  -f compose.ledger-tls.yaml -f compose.mcp.yaml up -d
```

成员客户端使用 `https://coordinator.example.org:8443/mcp` 与自己的 bearer 令牌。`BACKBONE_MCP_ALLOWED_HOSTS` 是以逗号分隔的**精确 Host header** 列表，含非默认端口；仅本机回环访问时可不设置。直接执行 `backbone serve` 也可用 `BACKBONE_MCP_HTTP=1` 和此环境变量，或使用同等 CLI 标志。服务会拒绝无令牌、非成员和未知 Host 的请求；轮换令牌无需重启。每位远程成员应使用独立的非特权运行账户和 bearer 令牌；客户端不应挂载协调仓库或服务端凭据。部署者应核对认证、冒名拒绝、Git 审计归属和重启恢复。

### 远程成员 CLI

仅需安装 Backbone 包、私有成员令牌和可信 HTTPS 的成员，可直接操作 HTTP 接口，不需要本地协调仓库或 DSH 模型。`--url` 是服务 origin，**不含** `/mcp`；下例中的 `TASK_ID` 来自 `tasks`，完整 `SHA` 来自成员代码克隆的 `git rev-parse HEAD`：

```sh
backbone member --url https://coordinator.example.org:8443 \
  --token-file /private/path/alice.token whoami
backbone member --url https://coordinator.example.org:8443 \
  --token-file /private/path/alice.token tasks
backbone member --url https://coordinator.example.org:8443 \
  --token-file /private/path/alice.token start TASK_ID
backbone member --url https://coordinator.example.org:8443 \
  --token-file /private/path/alice.token updates --since-version OBSERVED_VERSION
# 若决策上下文变化，先读取新的 tasks.version，再显式刷新任务：
backbone member --url https://coordinator.example.org:8443 \
  --token-file /private/path/alice.token rebase TASK_ID --version OBSERVED_VERSION
# 在独立克隆提交并推送 feature/alice 后：
backbone member --url https://coordinator.example.org:8443 \
  --token-file /private/path/alice.token fetch TASK_ID \
  --branch feature/alice --sha FULL_LOWERCASE_SHA
backbone member --url https://coordinator.example.org:8443 \
  --token-file /private/path/alice.token submit TASK_ID --file /private/path/artifact.json
```

`artifact.json` 是制品对象，至少包含 `intent_id`、`branch`（如 `origin/feature/alice`）、`base_ref`、`summary` 和固定的 `commit_sha`；`--file -` 可从 stdin 读取 JSON。成员也可用 `create-intent --file /private/path/intent.json` 建草稿、用 `propose-decision --file /private/path/decision.json` 提议决策。命令先调用 `/whoami`，只接受 member 角色，并把 author/member_id 绑定到令牌 principal；令牌从所有者持有的 0600 单链接文件读取。自签证书可加 `--ca-file`；非回环 HTTP 被拒绝。`updates` 只读取，不自动刷新任务；`rebase` 刷新 Backbone 决策与目标基线，不执行 Git rebase。提交制品不会合并代码或批准任务。

### 独立代码克隆的提交与合并

远程 MCP 和成员 HTTP CLI 只传递协调数据，不传送 Git 对象。管理员分派任务后，先把协调端目标代码分支推送到代码远端；独立元数据模式还需单独推送 `backbone` 分支。成员在自己的代码克隆中更新目标分支、通过成员 MCP 或 CLI 读取并开始任务、提交代码，再推送功能分支：

```sh
git -C /alice-worktree fetch origin main
git -C /alice-worktree switch main
git -C /alice-worktree merge --ff-only origin/main
git -C /alice-worktree switch -c feature/alice
# 在工作树内编辑并提交代码
git -C /alice-worktree push origin HEAD:refs/heads/feature/alice
```

成员在推送后通过 `/mcp` 的 `fetch_artifact_branch` 或远程成员 CLI 的 `fetch` 提供 `task_id`、功能分支名 `feature/alice` 和本地 `git rev-parse HEAD` 得到的完整小写 SHA。协调端仅从已配置的 Git remote（默认 `origin`）获取该分支，核对远端当前 tip 与预期 SHA，并仅快进 `origin/feature/alice` 跟踪引用；SHA 不匹配或远端强推改写时拒绝，且不更新跟踪引用。也可由管理员执行下方 Git 命令作为手工恢复步骤。此操作不产生 Backbone 审计提交，也不切换协调端 HEAD。

```sh
git -C /coordinator fetch origin \
  refs/heads/feature/alice:refs/remotes/origin/feature/alice
```

成员通过 `/mcp` 的 `submit_artifact` 或远程成员 CLI 的 `submit` 提供 `intent_id`、`branch`（例如 `origin/feature/alice`）、`base_ref`（例如分派目标 `main`）、`summary` 和刚获取的 `commit_sha`。协调端从 Git 解析真实提交和路径，检查范围与决策；请求中的 SHA 只用于核对，不代替 Git 证据。收到 `accepted: true` 后，审查者可用 `GET /tasks/{task_id}/inspection`、管理员 MCP `inspect_task` 或本地 `backbone task inspect TASK_ID` 读取固定提交的代码差异、当前目标分支 SHA、分支是否变动、决策变化与阻塞冲突。默认响应最多展示 128 KiB 补丁并附完整补丁 SHA-256；若 `truncated=true`，审查者可用 HTTP `?full_patch=true`、本地 `task inspect TASK_ID --full` 或远程 `reviewer inspect TASK_ID --full` 获取上限内的完整补丁。代码合并前 `target_diff` 为 null；合并后它展示任务声明路径相对原始基线在当前目标提交中的净差异，附目标 SHA、哈希与截断标记。`--full` 同时取得固定产物与目标路径的完整差异；目标差异若超过 1 MB，完整读取会拒绝，须在 Git checkout 中审阅。远程 CLI 会核对两份完整补丁与服务端哈希一致；该核对不能独立证明服务端 Git 状态，也不覆盖任务路径以外的目标树。超过 1 MB 的产物差异需拆分任务后重新提交。此接口只读，也不代替人工语义审阅。人工实际合并代码后，审查者最后以自己的 bearer 凭据调用 `POST /tasks/{task_id}/merge`，提交 `{"author":"reviewer","rationale":"...","expected_version":"...","expected_target_sha":"..."}`，两个预期值须取自合并后重新获取的审查包。未完成 Git 合并、账本或目标分支在检查后变化时该调用会被拒绝。合并和完成记录生成后再推送目标分支。已完成任务的 `inspect` 使用结构化审批锚点重新读取审批时的账本快照与目标提交，不把后续分支变化伪装成批准时的代码；响应另列 `current_version` 和 `git.current_target_sha`。旧完成记录缺少锚点时须查 Git 审计历史；原目标 Git 对象若已被删除，补丁无法重建。成员功能分支若有新提交，必须重新进行检查和审阅。

完成记录除检查固定提交已成为目标分支祖先，还会比较产物声明的代码路径：若这些路径相对提交时基线完全没有净改动，即使 Git 历史含产物提交（例如 `git merge -s ours`），也拒绝完成。若目标树在相关路径上与原产物不同，审查者须提交非空复核理由；记录会保存产物 SHA、目标 SHA 与差异路径。这是明显丢弃改动的确定性防线，不证明较复杂的同路径改写仍保留了原意图。审查者必须核对最终代码。合并后若代码已回退到基线，任务可取消或刷新后重新提交，避免只因 Git 祖先关系而无法恢复。

完成请求中的账本版本与目标 SHA 是并发检查值，不是已做过人工审查的证明。独立元数据分支模式无法把代码分支与元数据分支两次 Git 引用更新做成跨引用原子事务；审批窗口内应暂停其他进程对协调端代码 checkout 的写入，完成后再推送和继续合并。



### 无协调仓库的远程审查 CLI

审查者可在自己的机器上安装 Backbone Conductor，只保存自己的 bearer 令牌，不需要协调端 Git checkout。把一次性收到的令牌单独存成**仅包含令牌的一行**、由当前用户持有的 0600 普通文件；不要将文件放进 Git 仓库或共享目录。`--url` 指向服务根地址，不含 `/mcp`；非回环 HTTP 会被拒绝。自有 CA 证书可用 `--ca-file` 指定，默认使用系统信任库。

```sh
backbone reviewer --url https://coordinator.example.org:8443 \
  --token-file /private/path/carol.token whoami
backbone reviewer --url https://coordinator.example.org:8443 \
  --token-file /private/path/carol.token state
backbone reviewer --url https://coordinator.example.org:8443 \
  --token-file /private/path/carol.token intents
backbone reviewer --url https://coordinator.example.org:8443 \
  --token-file /private/path/carol.token review-intent INTENT_ID \
  --outcome accepted --rationale '目标与范围已核对' --version OBSERVED_VERSION
backbone reviewer --url https://coordinator.example.org:8443 \
  --token-file /private/path/carol.token decisions
backbone reviewer --url https://coordinator.example.org:8443 \
  --token-file /private/path/carol.token revert-decision DECISION_ID \
  --rationale '此前决策不再成立' --version OBSERVED_VERSION
backbone reviewer --url https://coordinator.example.org:8443 \
  --token-file /private/path/carol.token conflicts
backbone reviewer --url https://coordinator.example.org:8443 \
  --token-file /private/path/carol.token timeline \
  --http-principal carol --type conflict --limit 20
backbone reviewer --url https://coordinator.example.org:8443 \
  --token-file /private/path/carol.token audit-verify \
  --limit 50 --require-signatures
backbone reviewer --url https://coordinator.example.org:8443 \
  --token-file /private/path/carol.token audit-snapshot
backbone reviewer --url https://coordinator.example.org:8443 \
  --token-file /private/path/carol.token audit-history --all --limit 1000
backbone reviewer --url https://coordinator.example.org:8443 \
  --token-file /private/path/carol.token resolve-conflict CONFLICT_ID \
  --action coordinate --rationale '先统一接口变更顺序' --version OBSERVED_VERSION
backbone reviewer --url https://coordinator.example.org:8443 \
  --token-file /private/path/carol.token inspect TASK_ID --full --format text
# 管理员在协调代码仓库中审阅并真正执行 Git 合并后：
backbone reviewer --url https://coordinator.example.org:8443 \
  --token-file /private/path/carol.token inspect TASK_ID --full
backbone reviewer --url https://coordinator.example.org:8443 \
  --token-file /private/path/carol.token approve TASK_ID \
  --rationale '已核对最终代码与当前决策' \
  --version OBSERVED_VERSION --target-sha OBSERVED_TARGET_SHA
```

`state` 返回用于意图审查、决策撤销和冲突裁决的当前 `version`；若期间有人写入，旧版本会被拒绝，应重新读取状态和目标后审查。`decisions` 和 `conflicts` 提供撤销或裁决前的当前证据；`revert-decision` 只撤销已接受决策，不回滚代码或恢复被覆盖的前任，`resolve-conflict` 只裁决仍未解决的精确冲突 ID，并在审计记录中留下所审阅的版本。`inspect` 默认返回包含完整上下文的 JSON 审查包；`--format text` 汇总意图、任务约束、决策变化与阻塞冲突，并将补丁逐行显示、转义终端控制字符、明确标记截断预览；完整结构化证据仍须在 JSON 中核对。预览截断时用 `inspect TASK_ID --full` 读取最多 1 MB 的完整补丁，CLI 校验补丁与服务端哈希一致。合并后 `target_diff` 提供任务路径相对提交时基线的净差异，并绑定 `git.target_sha`；它不覆盖目标树的其他路径。超过 1 MB 的目标差异须在 Git checkout 中审阅，远程 `--full` 会拒绝将截断内容冒称为完整结果。代码合并后须重新运行 `inspect`，从该次响应复制 `version` 和 `git.target_sha` 给 `approve`；若账本或目标分支随后改变，审批会拒绝。审查者须核对完整代码与最终目标树，不能仅凭截断预览审批；`approve` 只记录审阅结论，不执行 Git 合并。审批后仍可用 `inspect TASK_ID --full` 复核当时的固定账本和目标代码；此时 `inspection_kind=approval`，`version` 与 `git.target_sha` 指向审批时快照，`current_version` 和 `git.current_target_sha` 说明现在的状态。命令先向 `/whoami` 核对令牌确属审查者，写入请求的 author 自动使用服务端返回的 principal。`timeline` 及 `audit-verify` 读取协调端 Git 账本与签名报告；后者依赖服务端的 Git 信任库，客户端没有独立验证提交签名，`--require-signatures` 只要求本次所检查的提交全部有效，仍需注意 `truncated`。

## 独立元数据分支

新仓库至少要有一个代码提交，且不能已经有内联 `.backbone/state.json`。执行 `backbone --repo /repo ledger create` 后，元数据保存在 Git 管理目录的隐藏 `backbone-ledger` worktree 中，当前代码工作树不切分支。所有协调命令都要显式使用 `--ledger-branch backbone`；例如 `backbone --repo /repo --ledger-branch backbone sync` 只推送元数据分支。其他克隆在已有远端 `backbone` 分支时运行 `backbone --repo /clone ledger attach`。成员提交的代码分支必须可被协调端的代码工作树解析；代码推送仍走 Git 常规流程。

现有内联仓库可在协作者暂停写入时运行 `backbone --repo /repo ledger migrate`。迁移要求当前代码分支有提交、工作树和索引干净、所有任务已完成或取消，且不存在本地或远端跟踪的 `backbone` 分支。它创建以旧代码 HEAD 为父提交的元数据专用分支，再在代码分支提交删除旧 `.backbone/`；旧审计提交仍在新分支祖先中。迁移成功后使用 `--ledger-branch backbone`，分别推送代码分支及元数据分支，再让其他克隆拉取代码并 `ledger attach`。若提交已经完成但 worktree 附加失败，运行 `ledger attach` 修复；不要重新迁移或强推历史。迁移不会自动推送。

隐藏 worktree 的 `.git` 指针与创建时的绝对路径绑定。同一挂载仓库在另一宿主路径直接使用隐藏 worktree 可能失败，因为 `.git` 指针保存绝对路径。为协调端使用专用 checkout，并在容器内创建或附加 worktree。`ledger create` 不会自动迁移现有内联审计历史。

## 审计与恢复

在需要签名的协调仓库中，先按 [Git 的提交签名文档](https://git-scm.com/docs/git-commit-tree) 配置可用的 GPG 或 SSH 私钥与可信公钥。SSH 示例中的私钥和 allowed signers 文件应放在仓库外，并限制文件访问：

```sh
git -C /repo config gpg.format ssh
git -C /repo config user.signingkey /private/path/audit-signing-key
git -C /repo config gpg.ssh.allowedSignersFile /private/path/allowed-signers
git -C /repo config commit.gpgsign true
backbone --repo /repo audit verify --limit 50 --require-signatures
backbone --repo /repo audit verify-snapshot
backbone --repo /repo audit verify-history --all --limit 1000
```

Backbone 用 `git commit-tree` 写元数据，启用 `commit.gpgsign=true` 时会显式传入 `-S`；签名失败则回滚该次元数据事务。`audit verify` 使用 Git 当前配置的信任库验证最近 `--limit` 个元数据提交，返回 `valid`、`unsigned`、`invalid`、`total_metadata_commits` 与 `truncated`。默认模式对无效签名返回非零状态；`--require-signatures` 还要求所检查提交全部有效。`truncated=true` 表示仍有更早提交未检查；旧提交不会因开启签名而补签。独立元数据分支使用 `--ledger-branch backbone` 检查该分支。Git 签名只证明某可信密钥签过提交，不能证明 HTTP principal、Git author 或自然人身份；allowed signers 的维护与私钥保护由部署者负责。验证命令不修改仓库。

`audit verify-snapshot` 另行核对**当前** `state.json`、所有生成视图以及元数据提交在第一/第二 Git 父链上的版本链接；缺失、多余、内容不一致或父版本错误都会使退出码非零。审查者可用 `reviewer audit-snapshot` 从认证服务读取同一报告，也可调用 `GET /audit/snapshot`；远程 CLI 只校验响应结构，不能独立验证服务端仓库。该命令不遍历并证明整个历史，不检查签名，也不能证明 HTTP principal 或真人身份。独立元数据分支请同样传入 `--ledger-branch backbone`。

`audit verify-history` 逐个检查**当前 HEAD 可达**、涉及 `.backbone/` 的元数据提交，比较其状态生成的文件集合、常规文件模式与 Git blob 哈希，并核对普通及双父提交的父版本链接。默认单页最多 50 个，可用 `--limit` 调整到 1000；单页结果提供 `offset`、`page_ok`、`next_offset` 和 `truncated`，只有从第一页开始且未截断的单页 `ok=true` 才表示整条可达历史通过。`--all` 用相同页大小遍历整条历史，固定首次读取的 Git HEAD，页间 HEAD 变化则拒绝继续；汇总报告只保留失败提交，`ok=true` 才表示全部已检查提交通过。需要手工分页时，把前一页的 `next_offset` 与 `head` 分别作为 `--offset` 和 `--expected-head` 传入。审查者可用 `reviewer audit-history` 或 `GET /audit/history` 读取服务端报告；远程 CLI 校验每页结构、计数及 HEAD 一致性，但不独立读取 Git 对象。它不验证签名，也无法发现已被整体改写且不再从当前 HEAD 可达的历史；应另行保管可信提交 SHA 并核对签名。独立元数据分支需指定 `--ledger-branch backbone`。

- `backbone log` / `git log -- .backbone` 查看历史。`backbone log` 与 HTTP `/timeline` 可按精确 Git author、已认证 HTTP principal、事件类型及带时区的 `since` / `until` 时间筛选，`limit` 在筛选后生效。时间依据 Git author timestamp，事件类型由提交主题归类，均为浏览索引而非独立验证的事实。已认证 HTTP 写入会在提交中留下 `Backbone-HTTP-Principal` 与 `Backbone-HTTP-Role` trailer，返回 `http_principal` / `http_role`（包括审查者）；未认证本地调用没有这些字段。它们记录服务端已验证的 bearer principal，不是 Git 签名，也无法阻止拥有仓库写权限的人伪造提交；Git author 仍由仓库配置决定。
- `backbone decision transition DECISION_ID reverted` 撤销接受过的决策并重算冲突。
- 整体回退需先停服务和备份，查看差异后 `git revert <metadata-commit>`；再确认 backbone status。首选领域命令，整提交回退可能改变多个对象。
- state.json 是权威快照，其他文件是生成视图。未提交的手工修改会被拒绝；修复时先停服务、检查并提交一致快照。
- `sync` 仅 push；`refresh` 显式 fetch。若本地落后、同名分支且工作树/索引干净，`refresh` 会快进并验证新快照；本地领先则不变更。双方分叉时返回共同祖先、两侧对象与路径差异及重叠项，不改写历史。维护者审查两个 HEAD 后，对仅元数据的分叉可执行 `backbone reconcile --local-head ... --remote-head ... --author ... --rationale ...`。同一对象两侧变化时，先从 `requires_review` 响应取得对象 ID，审阅两侧快照，再在命令中增加 `--resolutions-file`：每个竞争对象须选 `{"source":"local"}`、`{"source":"remote"}` 或提供 `{"value":{...完整对象...}}`。接口会拒绝遗漏或多余决议，校验合并状态，生成双父提交并重算冲突，随后显式 `sync` 推送；SHA 已变化、代码路径改动或跨对象生命周期不一致时拒绝合并。代码路径变化需走常规 Git 人工合并，不得强推审计历史。
- 成员分支不得改写 `.backbone/`。内联模式在目标分支协调；独立模式在 `backbone` 分支协调，代码操作仍针对源工作树。
- 锁被占用时先确认活跃 Git/协调进程，不盲目删除锁。

已验证 macOS/Linux 风格工作树与 linked worktree。Windows、网络文件系统和突然断电恢复未专门验证。进程内异常可回滚；异常断电后的差异需借助 Git 历史人工恢复。
