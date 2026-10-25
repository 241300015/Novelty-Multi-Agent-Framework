"""查新任务的解释语言约定，与文献检索语言独立。"""

CHINESE_EXPLANATIONS = (
    "输出语言要求：即使检索范围、输入论文或 ResearchTask.language 为英文，"
    "模型生成的综述、贡献说明、异同比较、判断理由、解释及面向用户的说明均使用简体中文。"
    "检索词与检索式仍使用任务指定的检索语言；文献题名、作者、专有名称、URL、DOI、"
    "原文引句和 exact_quote 保持原文，引用不得翻译或改写。"
    "JSON 字段名、枚举值、ID、工具名、选项标签及明确要求英文的 claim_en 等字段保持契约格式。"
)


def with_chinese_explanations(system: str) -> str:
    """在模型调用边界追加约定，覆盖文件模板和内置备用提示词。"""
    return system if CHINESE_EXPLANATIONS in system else f"{system}\n\n{CHINESE_EXPLANATIONS}"
