# 已有真实材料的 Renderer 三组验收

2026-10-19（Asia/Shanghai）完成，结果 **PASS**。本轮只使用已有材料，没有调用模型或网络，没有修改产品代码及模型配置。

后续又完成两份含有效证据卡的历史工作流编译，报告入口及核验结果见 [追加验收](../additional-completed/README.md)。

## 来源与兼容性

两份材料属于同一篇论文 `MF2033k6lC`，原始输入 SHA-256 相同。两份运行清单记录的代码提交均为 `d7948cc455ceb235a23e448f056859d993b76ee5`，历史模型均为本地 `qwen2.5-7b-instruct`。

| 组别 | 历史运行目录 | 材料所属时间（北京时间） | 查新点／结论 |
| --- | --- | --- | --- |
| A | `outputs/validator-gate-a-local-20261016/runs/0001` | 10 月 16 日 17:39:57–17:46:13 | 2／2 |
| B | `outputs/guided-reading-20261016/runs/0001` | 10 月 16 日 18:29:18–18:34:23 | 4／4 |

两次历史完整流程均为 `SUCCESS`，包含成功的合成、完整性检查、报告落盘和渲染阶段。本轮核对最终 `persist_report` 的运行状态快照与论文、查新点、任务／规划、证据、评审和报告文件；通过当前代码的 Pydantic 数据契约和 `validate_report_integrity`。两份材料均没有有效证据卡，结论保留“证据不足”，未补造业务事实。

验收代码为当前分支 `35e7a0b55b50814d7f4ad20ce951c1679401e9ab`。材料源于相同的历史提交且与当前输入结构兼容；不表示它们是在当前 HEAD 下重新运行完整 Agent 流程所得。

## 版本索引处理

历史材料早于本分支的版本门控，没有原生 `report-inputs.json`。`replay_existing.py` 在核对来源和契约后，只在独立测试副本中登记原始文件的 SHA-256 与依赖，补建测试索引；业务文件逐字节保持不变。材料时间取历史运行清单的开始和结束时间，索引另外记录本次导入时间、来源路径、来源提交，以及 `historical_write_time_index=false`。

这验证了已核对来源的历史快照进入当前编译链后的门控行为，不证明历史文件当初写入时已经受新门控保护。原目录没有补索引或改写文件。混合材料在 A 的索引封存后才替换文件，未重新登记或计算新索引。

## 验收结果

| 材料 | 结果 | 检查 |
| --- | --- | --- |
| A 完整材料 | 通过 | 新进程编译 default；13 个附件链接均有效；无遗留模板占位符 |
| B 完整材料 | 通过 | 新进程编译 default；13 个附件链接均有效；无遗留模板占位符 |
| A/B 混合材料 | 按预期拒绝 | 保留 A 索引，替换 B 的规划、评审、报告；报错 `报告产物版本不一致：retrieval-plans.json`；未生成报告 |

A、B 各重复编译一次：报告生成时间更新，材料所属时间、业务内容和版本索引不变。显示时间使用明确的 `+00:00` 偏移；本轮报告生成时间对应北京时间 10 月 19 日 01:00:23–01:00:26。

完整结构化证据见 [result.json](result.json)。三个归档包含所有登记的 Renderer 输入和索引，A、B 还包含生成的 `report/acceptance.md`，解压后可查看相对附件链接：

- [A 完整材料与报告](materials-A.tar.gz)
- [B 完整材料与报告](materials-B.tar.gz)
- [A/B 混合材料](materials-mixed.tar.gz)

原始 Runtime 和下载的参考文献正文保留在历史运行目录；归档不是完整 Runtime 备份。当前工作区可直接查看：

- [A 报告](../reports/A/MF2033k6lC/report/acceptance.md)
- [B 报告](../reports/B/MF2033k6lC/report/acceptance.md)

## 复现

在仓库根目录执行，使用新的工作目录以保留已有证据。脚本及编译子进程通过 Python audit hook 拒绝 socket 网络操作。

```bash
/home/lya3106643285/miniconda3/envs/Novelty/bin/python \
  docs/experiments/20261019_renderer_versions/replay_existing.py \
  --run-a outputs/validator-gate-a-local-20261016/runs/0001 \
  --run-b outputs/guided-reading-20261016/runs/0001 \
  --work-root outputs/renderer-versions-20261019/existing-materials-replay \
  --result outputs/renderer-versions-20261019/existing-materials-replay/result.json
```

这次完成的是“历史来源核验 → 当前契约与报告完整性校验 → 副本导入封存 → 版本门控 → default 编译 → 重复编译／混用拦截”的离线流程。此前新跑的本地流程仍记录为 Coordinator 合成失败，未被本次结果改写为成功。
