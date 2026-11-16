---
name: search_planner.semantic_facts
version: 1
system: |
  只从point中的查新点和技术特征提取原子技术事实，不存在候选概念/策略供参考。每条事实保留主体、动作、对象和必要条件；证据只能选source_catalog发放的ID。
  action可为uses使用、removes去除、retains保留、combines结合、targets目标、produces结果、context背景。去掉非线性激活不能写成使用非线性激活。
  defining表示能区别于普通方法的贡献机制；supporting是允许在放宽检索中省略的实现细节；context是对象、目标、数据集或效果。不要把每个词、每个细节都标为defining，也不要只提取模型名字或研究目标。
  research_goal准确表述研究对象和任务。最多16条事实，尽量精简。中英文术语可保留，原文不能支持的内容不添加。
---
任务输入
{payload_json}

输出JSON契约
{schema_json}
