# 确定性冲突规则

| 规则 | 依据 | 严重级别 |
| --- | --- | --- |
| replace_vs_extend | 同符号分别声明 remove/replace 与 extend | remove critical，replace blocking |
| symbol_scope_overlap | 活跃意图有共同符号，未被上一规则覆盖 | advisory |
| dependency_conflict | 一个决策 depends_on 与另一个 removes_symbols 相交 | blocking |
| resource_contention | 意图/任务声明或实际提交相同路径，或目录与子路径相交 | blocking |

终态意图、撤销/替代决策、merged 任务不再产生新冲突。草稿和提议也参与预警。规则依据结构化声明，不能推断任意代码语义。

冲突 ID 取决于规则、级别、双方和证据。重复扫描不重复创建；人工裁决仅沿用到完全相同证据。自动标记为不再适用的冲突重新出现时恢复阻塞。

coordinate、accept_existing、override_existing、accept_risk 记录人工选择和理由并形成决策，本身不修改代码、意图或其他决策。advisory 不阻塞。

制品路径来自固定 Git 提交的 diff；关闭 rename detection，同时检查移动前后路径。使用完整路径段匹配，src/auth 不匹配 src/author.py。全局决策没有 related_intents 时，其冲突适用于所有任务。

运行 `python scripts/evaluate.py` 检查 24 个合成场景；其 precision/recall/F1 不证明真实项目检测率 ≥80%。

另有 [Git 合并案例](evals/GIT_MERGE_CASES.md)，用公开项目的分支路径和 Git 文本冲突标签检查 `resource_contention`。这些案例只说明路径预警相对文本冲突的表现，不能推出意图级准确率。
