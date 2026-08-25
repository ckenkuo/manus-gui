class ToolError(Exception):
    """当工具遇到错误时引发。"""

    def __init__(self, message):
        self.message = message


class OpenManusError(Exception):
    """所有 OpenManus 错误的基础异常"""


class TokenLimitExceeded(OpenManusError):
    """当超过 token 限制时引发的异常"""


class EmptyContentTruncated(OpenManusError):
    """推理链吃光了 max_tokens、正文返回空时引发（finish_reason="length"）。

    【为什么要单独一类而不复用 ValueError】这是确定性失败：同样的参数重发只会再
    烧一遍推理链，2026-08-22 属性审核实测白等 2 分 17 秒（6 次指数退避里的一次）。
    单独成类才能让 LLM.ask 的 retry 谓词把它排除在退避重试之外，改由就地抬高
    max_tokens 重发一次——那才是对症的处置（根因就是额度不足）。
    """
