"""模型调用统一错误码和是否可重试信息。"""


class ModelCallError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class InvalidStructuredOutput(ModelCallError):
    def __init__(self, message: str = "模型输出不符合目标 Schema") -> None:
        super().__init__(
            "invalid_structured_output",
            message,
            retryable=True,
        )
