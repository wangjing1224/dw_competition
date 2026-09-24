"""已核对的服务能力，不按模型名模糊猜测第三方网关的限制。"""

from dataclasses import dataclass
from math import ceil
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelCapabilities:
    max_context_tokens: int
    max_output_tokens: int
    image_tokens_per_image: int | None = None
    # 仅确认口径为本次全部输入（含缓存）时才启用服务端用量基准。
    input_usage_confirmed: bool = False
    # 部分平台的最大输入小于总窗口；尤其压缩调用可能越过业务软阈值。
    max_input_tokens: int | None = None


# 2026-09-24 核对官方文档；此表不意味着第三方同名模型具有相同能力。
# https://api-docs.deepseek.com/api/create-chat-completion/
# https://api-docs.deepseek.com/guides/vision/
SERVICE_CAPABILITIES = {
    ("deepseek", "https://api.deepseek.com/v1", model): ModelCapabilities(
        1_000_000, 393_216, 1024, True
    )
    for model in ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp")
}
# 文本预算能力；其他平台视觉上界尚未标定，遇到图片会明确阻塞而非套用 DS 的 1024。
# https://developers.openai.com/api/docs/models/gpt-5.1
SERVICE_CAPABILITIES.update(
    {
        ("openai", "https://api.openai.com/v1", model): ModelCapabilities(
            400_000, 128_000, input_usage_confirmed=True
        )
        for model in ("gpt-5.1", "gpt-5.1-2025-11-13")
    }
)
# https://help.aliyun.com/zh/model-studio/qwen3-6-plus
# https://help.aliyun.com/zh/model-studio/qwen-plus （北京支持工具调用）
SERVICE_CAPABILITIES.update(
    {
        (
            "bailian",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            model,
        ): ModelCapabilities(
            1_000_000, output, input_usage_confirmed=True, max_input_tokens=inputs
        )
        for model, output, inputs in (
            # 取思考/非思考输入上限中较小者，避免摘要请求越过接口硬上限。
            ("qwen3.6-plus", 65536, 983616),
            ("qwen3.6-plus-2026-04-02", 65536, 983616),
            ("qwen-plus", 32768, 995904),
        )
    }
)


def get_model_capabilities(settings) -> ModelCapabilities:
    url = urlsplit(settings.base_url.rstrip("/"))
    # 只规范官方 DS 的两个等价路径，不把第三方网关冒充官方能力。
    path = "/v1" if settings.platform == "deepseek" and not url.path else url.path
    key = (settings.platform, f"{url.scheme}://{url.netloc}{path}", settings.model)
    if key not in SERVICE_CAPABILITIES:
        raise ValueError("尚未登记当前模型服务的输出上限，请核实后补充能力表")
    capabilities = SERVICE_CAPABILITIES[key]
    if settings.context_window_tokens > capabilities.max_context_tokens:
        raise ValueError("配置的上下文容量超过已核实服务上限")
    return capabilities


def output_allowance(settings, input_tokens, capabilities, *, compaction=False):
    """业务请求使用预留；压缩请求允许越过软阈值，但必须满足硬容量。"""
    capacity = settings.context_window_tokens
    if capacity > capabilities.max_context_tokens:
        raise ValueError("配置的上下文容量超过已核实服务上限")
    if (
        capabilities.max_input_tokens is not None
        and input_tokens > capabilities.max_input_tokens
    ):
        raise ValueError("输入超过已核实的服务输入上限，不能继续发送")
    # 从预留中拿出窗口的 1% 给本地计数误差，不增加一组业务 YAML 参数。
    margin = max(1, ceil(capacity * 0.01))
    available = capacity - input_tokens - margin
    if not compaction:
        available = min(
            available,
            capacity - int(capacity * settings.compaction_trigger_ratio) - margin,
        )
    value = min(available, capabilities.max_output_tokens)
    if value < 1:
        raise ValueError("没有足够的输出与计数误差空间")
    return value
