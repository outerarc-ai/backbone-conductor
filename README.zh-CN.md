# Backbone Conductor：基于 Git 的意图与决策协作工具

[![CI](https://github.com/outerarc-ai/backbone-conductor/actions/workflows/ci.yml/badge.svg)](https://github.com/outerarc-ai/backbone-conductor/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/backbone-conductor.svg)](https://pypi.org/project/backbone-conductor/)
[![Python](https://img.shields.io/pypi/pyversions/backbone-conductor.svg)](https://pypi.org/project/backbone-conductor/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[English](README.md) · [文档](docs/README.md) · [参与贡献](CONTRIBUTING.md) · [更新记录](CHANGELOG.md)

Backbone Conductor 将意图、决策、任务分派与审查证据统一保存在 Git 中。团队可以先约定工作范围，再核对提交的代码是否符合约定，并把审批绑定到实际合入的目标提交。核心功能无需模型服务。

## 核心能力

- **共享意图：**在实施前记录目标、约束、涉及的符号和预期路径。
- **决策脉络：**关联意图与决策，在浏览器中查看决策的替代与撤销关系。
- **可核对的交付：**将任务绑定到明确的 Git 提交，检查声明范围并查看合入后的目标分支。
- **可审计的审批：**记录审批者、审阅时的账本版本和目标提交。
- **多种接入方式：**使用 CLI、HTTP API、MCP 工具或仓库提供的 Codex 插件；工作脉络图连接意图、任务、制品和审批。

确定性检查依据声明的范围和 Git 路径发现冲突线索，不能证明代码语义正确。可选的 DeepSeek Harness 建议不具有审批权限；决策、代码审查与 Git 合入由人负责。

## 安装

需要 Python 3.12+ 和 Git：

```sh
python -m pip install backbone-conductor==0.1.0
backbone --help
```

从源码运行示例需安装 [uv](https://docs.astral.sh/uv/)：

```sh
git clone https://github.com/outerarc-ai/backbone-conductor.git
cd backbone-conductor
uv sync --locked --group dev
uv run python examples/demo.py
uv run python examples/two_agent_demo.py --mcp
```

示例使用临时仓库，不修改当前检出目录。示例中的参与者和审批由脚本模拟。

## 在项目中使用

目标 Git 仓库需要先有初始提交：

```sh
backbone --repo /absolute/path/to/project init
backbone --repo /absolute/path/to/project status
backbone --repo /absolute/path/to/project serve
```

本地 API 位于 `http://127.0.0.1:8000/docs`。打开 `/decision-map` 浏览决策脉络，打开 `/lifecycle-map` 查看意图、任务、Git 制品与审批。两张图均为只读视图，支持搜索、拖拽和缩放。已认证成员只能查看其角色范围内的工作；审查者和管理员可查看全图。

典型流程：

1. 提出包含范围和约束的意图，由审查者接受。
2. 分派任务，在 Git 分支中实施。
3. 提交分支和准确的提交号作为制品，核对范围、决策及阻塞冲突。
4. 审查完整补丁，将代码合入目标分支。
5. 使用审阅时的账本版本和已合入的目标提交记录审批。

[Codex 插件指南](docs/CODEX_PLUGIN.md)说明插件安装与角色绑定的 MCP 接入；[运维指南](docs/OPERATIONS.md)说明远程认证、同步和恢复。

## 文档

| 指南 | 内容 |
| --- | --- |
| [文档索引](docs/README.md) | 全部公开文档入口 |
| [实现说明](docs/IMPLEMENTATION.md) | 架构和审查边界 |
| [运维指南](docs/OPERATIONS.md) | 部署、凭据、同步和恢复 |
| [MCP API](MCP_API.md) | 工具权限与传输方式 |
| [冲突规则](CONFLICT_RULES.md) | 确定性检查 |
| [DSH 集成](DSH_INTEGRATION.md) | 可选语义建议 |
| [验证指南](docs/VALIDATION.md) | 可复现的检查及证据边界 |
| [发布流程](docs/RELEASING.md) | 构建与发布 |

欢迎通过 Issues 和 Pull Requests 参与。开发设置与审查要求见 [贡献指南](CONTRIBUTING.md)。项目采用 [MIT 许可证](LICENSE)。
