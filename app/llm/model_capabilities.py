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


# 2026-09-24 核对官方文档；此表不意味着第三方同名模型具有相同能力。
# https://api-docs.deepseek.com/api/create-chat-completion/
# https://api-docs.deepseek.com/guides/vision/
SERVICE_CAPABILITIES = {
    ("https://api.deepseek.com", model): ModelCapabilities(
        1_000_000, 393_216, 1024, True
    )
    for model in ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp")
}


def get_model_capabilities(settings) -> ModelCapabilities:
    url = urlsplit(settings.base_url.rstrip("/"))
    if url.path not in {"", "/v1"}:
        raise ValueError("尚未登记该服务路径的上下文/输出能力")
    key = (f"{url.scheme}://{url.netloc}", settings.model)
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
