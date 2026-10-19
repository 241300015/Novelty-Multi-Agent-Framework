"""框架级异常。"""


class WorkflowExecutionError(RuntimeError):
    """当工作流无法继续生成合法结果时抛出。"""
