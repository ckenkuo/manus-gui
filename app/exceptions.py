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


class ModelNotMultimodalError(OpenManusError):
    """配置的模型不在 MULTIMODAL_MODELS 白名单，ask_with_images 进函数即抛。

    【为什么单独一类】这是「config.toml 的模型名与代码白名单没对齐」的确定性
    配置错误，重发多少次结果都一样。2026-09-11 实测教训：DeepSeek 官方改名
    （旧名 deepseek-v4-flash-vision-exp → deepseek-flash）后代码白名单跟着改、
    两台生产机的 config 没跟上，每个商品的 ⑥ 素材图都被这道闸拦下，却按
    ValueError 走满 tenacity 6 次退避 + 外层 3 轮重问（约 3 分钟/个），最后
    落库的错误是 RetryError[<Future ... raised ValueError>]——真因被包装完全
    吃掉，25 个商品排队等人工。单独成类后 retry 谓词直接放行（立即抛出），
    报错文案一眼可读。
    """
