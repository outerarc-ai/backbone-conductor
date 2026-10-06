# 前瞻意图冲突评测

本流程用事前记录的两个意图预测是否需要协调，再与事后**独立人工**标签比较。它与 [合成规则评测](conflicts.json) 和 [Git 合并案例](GIT_MERGE_CASES.md) 分开；后两者都不能测真实意图冲突检测率。没有按本流程形成的独立标签时，不应报告真实项目的精确率或召回率。

先写采样方案：项目范围、纳入/排除标准、连续收集的起止时间、任务对如何产生、标签定义，以及预期负例来源。不要按已知冲突或检测结果挑样本。每条 case 固定一个项目、同一代码基线的完整 Git SHA、采集时间和两份工作开始前的意图。意图字段遵循 `Intent` 模型，必须显式写 `id`、`author`、`created_at`；作者不同、状态为 `draft` 或 `accepted`，创建时间不晚于采集时间。路径和符号范围须按当时的计划声明，不能在完成代码后回填实际改动。

已有 Backbone 仓库可在工作开始、任务分派之前，从当前记录中一次采集**全部**合格配对并立即冻结预测：

```sh
python scripts/evaluate_prospective.py capture \
  --repo /absolute/project-checkout \
  --project owner/project \
  --sampling '事前固定的纳入窗口与所有候选配对规则' \
  --dataset /private/study/cases.json \
  --predictions /private/study/predictions.json
```

独立元数据分支增加 `--ledger-branch backbone`。`capture` 要求代码工作树干净，只选择当前 `draft` 或 `accepted`、**从未有任务记录**的意图，并生成不同作者之间的所有配对；若没有合格配对则失败。它从代码 HEAD 取得 `base_sha`，返回 Backbone 快照版本和两个文件的 SHA-256。输出必须是仓库外的绝对路径，以私有权限新建，不覆盖已有文件。采集在生成预测后再次核对代码 HEAD、工作树和 Backbone 版本；若期间变化，会删除本次新建的数据与预测文件。采样文字仍需人工预先制定；重复快照、事前已有仓库外代码工作、伪造作者或时间戳都不能靠该命令排除。生成后应立刻将数据与预测固定在私有证据仓库或可信时间戳存储，再开始工作。

数据文件放在适当的**私有**位置。例如：

```json
{
  "schema_version": 1,
  "sampling": "连续收集项目 X 在指定时间窗内的所有并行任务对；排除规则事前固定",
  "cases": [
    {
      "id": "project-x-pair-001",
      "project": "owner/project-x",
      "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "captured_at": "2026-09-29T10:00:00Z",
      "intents": [
        {
          "id": "intent-a",
          "author": "alice",
          "problem": "改变付款接口",
          "proposed_outcome": "新付款接口可用",
          "affected_paths": ["src/payment.py"],
          "created_at": "2026-09-29T09:00:00Z"
        },
        {
          "id": "intent-b",
          "author": "bob",
          "problem": "扩展付款调用方",
          "proposed_outcome": "调用方兼容新接口",
          "affected_paths": ["src/checkout.py"],
          "created_at": "2026-09-29T09:30:00Z"
        }
      ]
    }
  ]
}
```

以上仅是**格式示例**，SHA 与案例不是观察数据。工作开始前运行：

```sh
python scripts/evaluate_prospective.py freeze \
  --dataset /private/study/cases.json \
  --output /private/study/predictions.json
```

`freeze` 只创建新文件，不覆盖旧预测；它在规则检测前后及发布后核对数据文件哈希，也核对刚写出的预测文件字节；若采样文件或发布结果中途变化则拒绝并清理本次创建的无效预测。它记录原始数据文件和检测器及评测脚本源码的 SHA-256、冻结时间、每例规则与证据。把数据与预测提交到私有证据仓库或可信时间戳存储，记录提交 SHA，然后再开始工作。**自报时间和哈希不能单独证明冻结发生在工作之前**；保存可独立核对的外部时间顺序很重要。后续 `score` 不会重新运行当前版本的检测器，而是使用冻结的预测；修改原数据文件会被拒绝。本流程只评估两份事前意图之间的预警，不覆盖决策和任务规则。

若要同时评估可选 DSH 模型建议，先用 `uv sync --locked --group dev --extra dsh` 安装锁定 SDK，并在仓库外配置 provider 凭据及私有 DSH home。在工作开始及审阅者查看预测之前，使用**同一份**事前数据与确定性预测运行：

```sh
python scripts/evaluate_prospective.py freeze-semantic \
  --dataset /private/study/cases.json \
  --predictions /private/study/predictions.json \
  --output /private/study/semantic.json \
  --dsh-home /absolute/private-dsh-home --model YOUR_MODEL
```

此命令把两份意图的计划字段、代码基线 SHA 与已冻结的确定性证据逐例发送给配置的 provider；可能产生费用。模型**看得到规则证据**，因此后续“模型建议”指标并非独立的纯模型检测器指标。v1 前瞻数据不含已接受决策，也不能代表在线协调时含决策的完整建议质量。语义文件记录每例结构化意见、实际输入哈希、模型/提供商、完成状态和实现源码哈希；只有全部回合成功且原始数据及确定性预测在运行期间未改变，才以私有权限一次性创建文件；发布后再次核对输入与模型文件字节，不匹配时清理本次创建的文件。它不会修改 Backbone 仓库。将三份文件一起固定在外部可信证据存储，并记录冻结完成时间；模型调用和自报时间本身不能证明事前顺序。不可在已知工作结果后重新挑选或冻结案例。

工作结果可供审阅时，先制定标签定义：`conflict=true` 表示两份原计划若并行执行，需要在集成前协调范围、先后顺序或设计；`false` 表示不需要这种协调。文本 Git 合并冲突只是证据之一，不能自动充当标签。两位非意图作者的审阅者分别看到原意图、实际产物及必要上下文，但**不看预测或对方标签**。可分别生成私有盲审文件：

```sh
python scripts/evaluate_prospective.py prepare-review \
  --dataset /private/study/cases.json \
  --predictions /private/study/predictions.json \
  --reviewer carol --output /private/study/carol.json
python scripts/evaluate_prospective.py prepare-review \
  --dataset /private/study/cases.json \
  --predictions /private/study/predictions.json \
  --reviewer dave --output /private/study/dave.json
```

若冻结了可选模型建议，生成每份文件时都增加 `--semantic-predictions /private/study/semantic.json`。生成器检查冻结文件与案例是否匹配，拒绝意图作者作为审阅者，以私有 0600 权限新建文件且不覆盖已有标签；发布后再次核对输入与盲审文件，不匹配时只清理本次创建的文件。人工编辑后也须保持文件私有。文件包含采样案例、基线 SHA、事前意图计划和判定定义，只有预测文件的 SHA-256，**不含规则证据、预警或模型意见**；不要把预测文件或另一位审阅者的标签交给审阅者。案例 ID 或原意图若本身暗示结果，工具无法消除这种盲审偏差，采样时须避免。实际产物与必要上下文仍须另行提供，且不能夹带预测。审阅者将每例的 `conflict: null` 改为 `true` 或 `false`，填写非空 `rationale`；未标注案例保持 `null` 与空理由。评分器会核对文件中的案例内容与冻结数据，原计划被修改会拒绝；空标签只计为未完成，不计为负例。

也可手工建立兼容的旧格式审阅文件。每人单独写一个文件；文件中的 `dataset_sha256` 与 `predictions_sha256` 是绑定值，可从冻结文件计算，不需要展示预测内容。旧格式文件形如：

```json
{
  "schema_version": 1,
  "dataset_sha256": "填入 cases.json 的 SHA-256",
  "predictions_sha256": "填入 predictions.json 的 SHA-256",
  "reviewer": "carol",
  "cases": [
    {
      "id": "project-x-pair-001",
      "conflict": true,
      "rationale": "两项变更对付款接口的兼容策略需要共同决定"
    }
  ]
}
```

若两人不同意，第三位非作者、非前两位审阅者可按相同格式提供裁决文件。未标注、只收到一份标签、或有分歧但无裁决的案例保留为 `incomplete`，不被默认为负例。运行：

```sh
python scripts/evaluate_prospective.py score \
  --dataset /private/study/cases.json \
  --predictions /private/study/predictions.json \
  --review /private/study/carol.json \
  --review /private/study/dave.json \
  --adjudications /private/study/erin.json --json
```

若冻结了 `semantic.json`，生成器会把模型文件的哈希绑定到盲审文件；手工旧格式审阅及裁决文件则需额外加入 `"semantic_predictions_sha256": "填入 semantic.json 的 SHA-256"`。审阅者只获得这些哈希绑定值，不看预测内容；评分时增加 `--semantic-predictions /private/study/semantic.json`。报告分开给出确定性规则、看过规则证据的模型建议、两者 OR 联合预警的 TP/FP/FN/TN、精确率与召回率，不会以模型的 `compatible` 取消规则预警。`uncertain` 记为没有发出模型预警；若人工标签为正例，就计入模型建议的漏报，并另报弃答数。只有双人一致或经第三人裁决的案例进入任何一组指标；因此标签不足时的指标只覆盖已解决子集。

没有分歧时省略 `--adjudications`。报告列出已解决样本的 TP/FP/FN/TN、精确率、召回率、F1 和逐例状态；没有正例时召回率为 `null`，没有预警时精确率为 `null`。`labeling` 分别记录两人的已标注数、双人已标注数、同意/分歧数、已裁决/未裁决分歧数，以及只以双人已标注案例为分母的原始一致率；没有双人标签时一致率为 `null`。原始一致率不校正偶然一致，也不能证明审阅者独立。`project_summaries` 对每个纳入项目分别列出总案例数、已解决数和只基于已解决子集的确定性规则指标；提供模型预测时也列出语义及联合预警指标。比较项目表现时应同时看各项目未解决案例数和样本量，不能只看总体精确率。

报告记录按 `--review` 顺序对应的 `first_review_sha256`、`second_review_sha256`，以及提供裁决时的 `adjudications_sha256`；评分完成前会重新核对这些文件，若过程中内容改变则拒绝出具报告。保存报告时须同时私下保留对应的原始标签文件，以便按哈希核验；哈希本身不能证明审阅者身份或独立性。`complete_sample` 只说明此文件的案例标签齐备，不证明抽样代表性或审阅者确为不同自然人。发布指标前应检查时间顺序、采样偏差、标签一致性和各项目分布，并保留未解决样本及理由。不要把敏感任务、代码或审阅记录直接提交到本公开仓库。
