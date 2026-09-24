"""按 YAML 中的职责配置创建 LangChain 模型注册表。"""

from dataclasses import dataclass

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.core.config import (
    AppSettings,
    ChatModelSettings,
    EmbeddingSettings,
    get_settings,
)
from app.llm.embedding_model import EmbeddingModel
from app.llm.rerank_model import RerankModel


def budgeted_generation_options(settings: ChatModelSettings, tokens: int) -> dict:
    """仅已接入控制器的调用使用；不省略接口额度而退回服务默认值。"""
    return {
        "extra_body": {
            "max_tokens": tokens,
            "enable_thinking": settings.enable_thinking,
        }
    }


def build_chat_model(settings: ChatModelSettings) -> ChatOpenAI:
    api_key = settings.api_key.get_secret_value()
    if not api_key:
        raise ValueError(f"模型 {settings.model} 未配置 API Key")
    return ChatOpenAI(
        model=settings.model,
        base_url=settings.base_url.rstrip("/"),
        api_key=api_key,
        temperature=settings.temperature,
        timeout=settings.timeout_seconds,
        max_retries=settings.max_retries,
        # 算力网文档使用 max_tokens，并通过 enable_thinking 控制
        # Qwen3/DeepSeek-V4 的思考模式；extra_body 会把两项放到
        # OpenAI 兼容请求的顶层。
        extra_body={
            **(
                {"max_tokens": settings.max_output_tokens}
                if settings.max_output_tokens is not None
                else {}
            ),
            "enable_thinking": settings.enable_thinking,
        },
    )


def build_embedding_model(settings: EmbeddingSettings) -> EmbeddingModel:
    api_key = settings.api_key.get_secret_value()
    if not api_key:
        raise ValueError(f"Embedding 模型 {settings.model} 未配置 API Key")
    client = OpenAIEmbeddings(
        model=settings.model,
        base_url=settings.base_url.rstrip("/"),
        api_key=api_key,
        # 本地 llama.cpp 的 OpenAI 兼容 Embedding 接口不支持
        # dimensions 参数。配置中的 dimension 仅用于响应维度校验；
        # Qwen3-Embedding-0.6B 直接返回原生 1024 维向量。
        chunk_size=settings.batch_size,
        timeout=settings.timeout_seconds,
        max_retries=settings.max_retries,
        check_embedding_ctx_length=False,
    )
    return EmbeddingModel(
        client=client,
        dimension=settings.dimension,
        batch_size=settings.batch_size,
    )


def build_parser_vision_model(settings: AppSettings | None = None) -> ChatOpenAI:
    """惰性创建，原生解析和旧 Agent 注册表不要求视觉 Key。"""
    config = (settings or get_settings()).parser_vision_model
    if config is None or not config.base_url.strip() or not config.model.strip():
        raise ValueError("未配置 parser_vision_model 的连接地址或模型标识")
    return build_chat_model(config)


@dataclass(frozen=True)
class ModelRegistry:
    rule_extraction: ChatOpenAI
    evidence_classification: ChatOpenAI
    bid_summary: ChatOpenAI
    bid_chat: ChatOpenAI
    evidence_chat: ChatOpenAI
    embedding: EmbeddingModel
    rerank: RerankModel


def build_model_registry(settings: AppSettings | None = None) -> ModelRegistry:
    resolved = settings or get_settings()
    return ModelRegistry(
        rule_extraction=build_chat_model(resolved.rule_extraction_model),
        evidence_classification=build_chat_model(
            resolved.evidence_classification_model
        ),
        bid_summary=build_chat_model(resolved.bid_summary_model),
        bid_chat=build_chat_model(resolved.bid_chat_model),
        evidence_chat=build_chat_model(resolved.evidence_chat_model),
        embedding=build_embedding_model(resolved.embedding_model),
        rerank=RerankModel(resolved.rerank_model),
    )
