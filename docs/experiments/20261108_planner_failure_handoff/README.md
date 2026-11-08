# Planner 失败实验精选归档

这是历史失败与待验收证据，不是当前分支的验收结果。原始实验在 c9128c79 基础上的未提交实现上运行；该实现已冻结并回滚，不能只凭 HEAD 相同复现。

## 范围

- live-20261102-001：本地 Qwen，40 次实际派发，进入三个 DSGNN 中文任务，第三个被 STOP 中断；未执行后续矩阵和注入。
- retest-20261102-002：同模型、语义提示修订后，25 次派发，进入两个任务，第二个在初审前中断。两轮均有错误语义放行，整批未通过。
- pending-batches.json：另一路 GSAERU / Museformer 实验目录的状态摘要。completed_pending_human_review 不能解释为语义通过，也不能与上面两个批次相加形成通过率。

state.json 保留机械状态，包括 pending；root-semantic-review.json 是独立 Agent 的语义检查，不是用户人工签收。两种记录分开保留，不篡改历史。任务文件保留输入、已放行计划、摘要和首次复核响应（存在时）。manifest-excerpt.json 是注明来源摘要的字段选摘，不是完整可执行快照。

选摘保留相对路径与 SHA-256，见 provenance.json。完整模型请求、运行环境、凭据、机器地址、冻结代码包和无关日志不上传。本归档适于核对失败证据，不承诺独立重跑全部历史实验。

当前代码与后续架构说明见后续交接文档；行为目标见 ../../planner-two-stage-implementation-and-acceptance.md。
