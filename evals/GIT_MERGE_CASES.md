# Git 合并案例

`git_merge_cases.json` 固定四个公开项目的双父合并提交：三个来自 [jsoup](https://github.com/jhy/jsoup)，一个来自 [Amaze File Manager](https://github.com/TeamAmaze/AmazeFileManager)。两个正例也收录于 [ConflictBench](https://github.com/UBOWENVT/ConflictBench) 的真实合并场景目录。每条记录保留上游仓库、合并提交、两父提交、从共同祖先到每个父提交的路径列表，以及 `git merge-tree --write-tree` 是否报告文本冲突；不复制上游源码。

| 案例 | Git 回放标签 | 路径争用预警 | 对文本冲突标签的结果 |
| --- | --- | --- | --- |
| [jsoup `pom.xml`](https://github.com/jhy/jsoup/commit/dd8e832191e78a417a03c88512b1d7eeb7f486d4) | 冲突 | 有 | TP |
| [jsoup 同文件可合并](https://github.com/jhy/jsoup/commit/a44e18aa3c1fcd25a68a5965f9490d8f7d026509) | 干净 | 有 | FP |
| [jsoup 路径不重叠](https://github.com/jhy/jsoup/commit/38e20f43502027b897135e94e9119ae9341254ac) | 干净 | 无 | TN |
| [Amaze `MainActivity.java`](https://github.com/TeamAmaze/AmazeFileManager/commit/4ee2cdaf5fea7badeab009d4f1dc6ce276c60dbe) | 冲突 | 有 | TP |

离线运行 `python scripts/evaluate_git_merges.py`，得到 **TP 2、FP 1、FN 0、TN 1**，相对文本冲突标签的 precision 66.7%、recall 100%。脚本把两个分支实际修改的路径分别填入两个 Backbone 意图，仅检查 `resource_contention` 预警。它没有重放人类原始意图、符号声明或决策，因此不评价其他规则。

若要重新验证路径和标签，分别克隆上游 Git 仓库，再运行：

```sh
python scripts/evaluate_git_merges.py \
  --verify-source jhy/jsoup=/absolute/path/to/jsoup \
  --verify-source TeamAmaze/AmazeFileManager=/absolute/path/to/AmazeFileManager
```

验证模式逐条核对合并提交的父 SHA、共同祖先到各父提交的路径列表，以及 Git merge-tree 的返回状态；无须修改上游工作树。离线默认模式只评估已固定的数据文件，不会联网。四例有意覆盖文本冲突和同路径干净合并，不代表真实项目总体。这里的 FP 只相对**文本冲突**标签：同文件修改仍可能需要协调。路径列表是代码完成后的回顾性输入，不能代表分派前的意图声明。这组案例不能用于宣称意图冲突检测率；相关测量需事前记录意图并由独立审阅者标注冲突与非冲突。
