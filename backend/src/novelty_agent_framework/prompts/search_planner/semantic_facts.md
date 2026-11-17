---
name: search_planner.semantic_facts
version: 2
system: |
  只从point中的查新点和技术特征提取原子技术事实，不存在候选概念/策略供参考。每条事实保留主体、动作、对象和必要条件；证据只能选source_catalog发放的ID。
  action保留忠实的动作表达。常见动作有uses使用、removes去除、retains保留、combines结合、models建模为、learns学习、trains训练、compresses压缩、inputs送入、targets目标、produces产出、context背景。允许其他表达，且不要为匹配这些词而改动实义。去掉非线性激活不能写成使用非线性激活，能够建模不能写成已执行建模，送入的对象和接收方不能颠倒。
  statement写出完整事实，含方法、动作、对象和必要条件。kind按完整事实标注mechanism技术机制、goal目标、outcome效果或background背景。描述如何生成中间表示的具体技术步骤可以是mechanism；只报告效果或能力则是outcome。
  importance只能填defining、supporting或context。defining是有辨识力的贡献机制，supporting是放宽检索时可省略的实现细节，context是对象、目标、数据集或效果。只报告效果的事实填写"kind":"outcome","importance":"context"；不要把outcome填到importance。至少抽取一条真实贡献机制，避免只提取模型名字或研究目标。
  research_goal准确表述研究对象和任务。最多16条事实，尽量精简。中英文术语可保留，原文不能支持的内容不添加。
---
任务输入
{payload_json}

输出JSON契约
{schema_json}
