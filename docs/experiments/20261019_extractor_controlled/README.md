# Extractor Harness 本地实验与 P0-01 验收

日期：2026-10-19；分支：`experiment/extractor-harness-20261019`；基于远端 main 的
`ee5ca8b4`。本目录记录迭代过程，包括失败，不能把不同源码版本的运行合并为成功率。

## 合并范围

生产提交为 `2b9b9fb4`（阅读门控）、`47bbc2e1`（Harness 与默认配置）、
`c363e70f`（正式文档）。正式默认配置启用新 Harness，模型参数沿用已验证的 temperature=0、
max_tokens=4096。实验脚本、专用 profile、本目录及同日 runtime 归档仅保留在实验分支。
合并前验证 184 项通过，记录见 `merge-validation/`。以下内容是实验当时的历史状态。

## 结论

持久化阅读与有界纠错的工程闭环已通过本轮测试；本地模型的自主选段和语义审核仍有明确缺陷。
生成／审核分开提示词、独立预算、程序合法性检查、按菜单纠错及再次审核已启用实验入口，
尚不把单个通过案例当成模型稳定性或整篇贡献完整性认证。

所有推理均使用 `http://127.0.0.1:8000/v1` 的 `qwen2.5-7b-instruct`，
temperature=0、max_tokens=3072、单次 timeout=180 秒。生成、审核和阅读选择各最多 3 次；
Reader 最多 24 次尝试、20000 字符。角色使用同一本地模型、不同系统提示词。
实验 profile 默认输出上限为 4096；本轮隔离实验的实际 3072 参数以各目录 manifest 为准。

## 最终机制验证

| 运行 | 实际结果 | 调用数 | token 总数 | 时间（秒） |
| --- | --- | ---: | ---: | ---: |
| [final-tail](final-tail/result.json) | 自主补读合成样本后段，保留 HELIOS-LATE 并引用 `[8000:8351]` | 3 | 4456 | 9.44 |
| [final-real-tail](final-real-tail/result.json) | 真实长论文指定后段，生成 DSGNN 机制并引用 `[52000:56000]` | 3 | 19620 | 15.60 |
| [fault-duplicate-v5](fault-duplicate-v5/result.json) | 注入重复项→审核判重→受限合并→复审，2 项变 1 项 | 5 | 10471 | 26.08 |
| [final-invalid-reader](final-invalid-reader/result.json) | 未发放的 Reader 选项被拒绝，纠正后读取并完成生成审核 | 4 | 5604 | 11.11 |
| [final-invalid-generation](final-invalid-generation/result.json) | 生成 JSON 错误→生成角色纠正→审核 | 4 | 6318 | 18.14 |
| [final-invalid-review](final-invalid-review/result.json) | 审核 JSON 错误→审核角色纠正 | 4 | 7094 | 11.14 |
| [baseline-tail](baseline-tail/result.json) | 旧流程对照：要求全文却未选择有效段，纠正仍失败，生成未启动 | 3 | 3848 | 2.84 |

重复注入是在一次真实生成响应后复制首个候选并加上改写说明；三个合法性错误也在真实响应后注入。
`calls/*.json` 区分 `raw_model_output` 与 `harness_visible_output`，并标明注入。
这些是故障恢复实验，不是自然错误率统计。旧流程对照保留了本分支的前置阅读门控，
不是未经修改的远端基线；单次对照失败不能证明整体收益。

最终六个机制案例的 Harness 与四个提示词文件 SHA-256 均与当前实现一致。
审核模板为 `semantic_review.md` v6，生成 v3，阅读选择 v3，纠错程序 v2。
提示词版本和源文件在每个运行的 `manifest.json`、`source/` 中冻结。

## 后段阅读证据

合成样本为 8351 字符，HELIOS-LATE 不在原 digest 中。父进程仅持久化输入，
新解释器子进程只接收目录路径，重新打开 PaperInput，再执行阅读、生成与审核。
`final-tail/trace.json` 的生成请求包含后段正文，最终结果引用同一冻结输入范围。

真实样本为已有的 `tests/fixtures/extractor/real_paper_input.json`，
《面向大规模动态图的图神经网络优化机制研究》，全文 81079 字符、原解析元数据 90 页。
整个提取实验没有读取原 PDF，没有重复调用解析器。

`final-real-tail` 是**指定阅读范围的隔离验收**：评估器将首次阅读选择替换为程序发放的
`body-14` 选项，原模型选择单独保留。之后的生成、审核均为真实本地调用，不覆盖其响应。
这验证正文消费与引用，不验证模型能自主找到该段。

生成请求逐字包含 `PaperInput.full_text[52000:56000]`。该范围覆盖第 4.3.3 节及原文
`# Page 61`（印刷页 47）的机制描述：依据各 Sketch 的损失学习权重，再聚合训练结果以弥补
拓扑信息缺失。此段具体论述不在原 digest 摘录中。模型保留的技术特征是
“通过注意力机制聚合多个图摘要的结果以弥补原图拓扑信息的缺失”，来源指向该范围。
模型未保留损失加权公式细节，故不把本项算作完整方法重建；也不把 DSGNN 名称本身当成
只能从后段获得的信息。精确原文起止位置与检查结果见 [acceptance.json](acceptance.json)。

PDF 解析后输入／直接 PaperInput 两入口均在独立子进程拼回全文并执行提取，分别覆盖旧流程
与新 Harness，共四个用例。PDF 入口仅用现有 text_layer 解析器准备测试输入，持久化后删除
原 PDF，并在子进程禁止调用解析器。该测试证明持久化交接，不评价 MinerU/OCR 质量。

## 必须保留的失败结论

- `local-tail-v1` 至 `v4`：选项身份、审核互斥行为或无动作字段的契约不稳定。
  对应迭代收紧动态菜单、加入 schema 和后置检查、明确补读与裁定互斥。
- `real-paper-v1`：一次选择过多段，超过阅读预算，纠正后仍不合法；程序拒绝执行。
- `real-paper-v2/v3`：流程返回成功，但只读 `[0:4000]`，20 段未读，不能验收自主后段选取。
  v3 还把“平均内存消耗和训练时长只有原来的 9.86%/13.27%”写成“降低了
  9.86%/13.27%”，审核漏检。只通过候选不代表语义正确或全文完整。
- `fault-invalid_review-v1` 与 `fault-duplicate-v1`：审核提出无依据的补充，生成输出空特征；
  固定检查拒绝而终止。随后增加 `request_recheck`，允许生成器附理由交回审核，禁止凑点。
- `fault-duplicate-v2`：程序执行成功，但两个重复点均获通过，是**语义验收失败**。
  因此改为显式逐对比较，并校验比较结论与逐点问题一致。
- `fault-duplicate-v3`：完成合并后，复审虚构候选对占位项，纠正仍失败，程序停止。
- `fault-duplicate-v4`：选择 revise 只是改写重复点，复审仍发现重复并耗尽预算。
  最终菜单取消重复项的 revise/split，只允许撤回或合并；拒绝环形重复代表关系。

后置程序检查只能验证契约、操作权限、来源是否已读和审核内部一致性。
如果模型一致地给出错误语义判断，程序仍可能接受。一次 v5 成功不能抹去 v2 的漏判，
也不能推出任意论文上的高召回率、低误删率或低断点率。

## 自动化与复现

[regression.xml](regression.xml)：157 项通过、0 失败，涵盖提取、覆盖、持久化阅读、配置注入。
包含缺文件、哈希改变、身份／版本串用、伪造引用、预算耗尽、纠正失败、传输失败、非法修改、
补充项再次审核、重复比较一致性、同版本 Reader 重试、成功／失败轨迹落盘。
未把这组回归称为仓库所有测试。

```sh
python scripts/run_extractor_controlled_experiment.py --case long_tail --output /tmp/new-tail
python scripts/run_extractor_controlled_experiment.py --case real_paper --reader-segment body-14 --output /tmp/new-real-tail
python scripts/run_extractor_controlled_experiment.py --inject duplicate --output /tmp/new-duplicate
python scripts/audit_extractor_controlled_experiment.py docs/experiments/20261019_extractor_controlled
```

本机 Python 使用 `/home/lya3106643285/miniconda3/envs/Novelty/bin/python`。
每次选择新输出目录。审计脚本无需网络、模型或 PDF；目前 15 项存档检查通过。
`acceptance.json` 同时列出所有迭代的用量、范围和状态，状态不是语义效果分数。

## 追加全流程实验

配置为 `config/profiles/extractor-harness-full-local.json`，所有角色都使用本地 Qwen，
启用新 Extractor Harness；arXiv 为唯一实际检索源，最大 80 次模型调用、24 次物理检索请求、
单轮单并发。目标论文使用同一真实 PaperInput，未删减参考文献或修改正文。
`full-workflow/0001` 的配置预检已通过。首次真实启动因外发检索词授权范围被自动审批拒绝；
用户明确授权 arXiv 检索后，`full-workflow/0002` 已在 82.28 秒内完成并产出报告。
Extractor 3 次调用、无纠错或中断；全流程 8 次本地调用、41356 tokens，14 个执行阶段通过。
6 次 arXiv 物理检索返回 0 条候选，最终走证据不足路径，不能算实质证据阅读／审核验证。
拒绝发生在进程启动前，不计为 Extractor 运行失败。
逐阶段证据和报告见 [全流程实验](full-workflow/README.md)。

同一生产配置又做了 [5 次独立进程提取](stability/README.md)：5/5 完成、0 次中断，
1 次自然审核一致性错误被自动纠正，总计 16 次本地调用。该样本未观察到高频流程断点。
但五次均只读开头，且一次比例语义错误漏检；这个结论不能扩展为全文覆盖或普遍语义可靠性。

机制说明见 [Extractor 受控 Harness](../../extractor-controlled-harness.md)。
