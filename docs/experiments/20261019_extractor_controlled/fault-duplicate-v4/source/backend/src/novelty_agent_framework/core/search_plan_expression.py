"""SearchPlan expression DSL 的数据库无关解析与校验。"""

from __future__ import annotations

import re

_TOKEN = re.compile(
    r"C\d+(?![A-Za-z0-9_])|AND(?![A-Za-z0-9_])|OR(?![A-Za-z0-9_])|[()]"
)
_CONCEPT_ID = re.compile(r"C\d+")


class SearchPlanExpressionError(ValueError):
    """SearchPlan expression 不符合共享 DSL 契约。"""


def parse_search_plan_expression(
    expression: str,
    *,
    defined_concepts: set[str],
) -> tuple[str, ...]:
    """校验 expression 的 token、语法及 Concept 引用并返回 token 序列。"""

    tokens = _tokenize(expression)
    depth = 0
    expects_operand = True

    for token in tokens:
        if expects_operand:
            if token == "(":
                depth += 1
            elif _CONCEPT_ID.fullmatch(token):
                if token not in defined_concepts:
                    raise SearchPlanExpressionError(
                        f"检索表达式引用了未定义 Concept：{token}"
                    )
                expects_operand = False
            else:
                raise SearchPlanExpressionError(
                    f"检索表达式在 {token!r} 前缺少 Concept 或 '('"
                )
            continue

        if token in {"AND", "OR"}:
            expects_operand = True
        elif token == ")":
            if depth == 0:
                raise SearchPlanExpressionError("检索表达式括号不平衡")
            depth -= 1
        else:
            raise SearchPlanExpressionError(
                f"检索表达式在 {token!r} 前缺少 AND/OR"
            )

    if expects_operand:
        raise SearchPlanExpressionError("检索表达式不完整")
    if depth:
        raise SearchPlanExpressionError("检索表达式括号不平衡")
    return tokens


def _tokenize(expression: str) -> tuple[str, ...]:
    tokens: list[str] = []
    position = 0
    while position < len(expression):
        if expression[position].isspace():
            position += 1
            continue
        match = _TOKEN.match(expression, position)
        if match is None:
            fragment = expression[position : position + 16].split(maxsplit=1)[0]
            raise SearchPlanExpressionError(
                f"检索表达式包含不支持的 token：{fragment!r}"
            )
        tokens.append(match.group())
        position = match.end()
    if not tokens:
        raise SearchPlanExpressionError("检索表达式不能为空")
    return tuple(tokens)
