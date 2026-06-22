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


def build_chat_model(settings: ChatModelSettings) -> ChatOpenAI:
    api_key = settings.api_key.get_secret_value()
    if not api_key:
        raise ValueError(f"模型 {settings.model} 未配置 API Key")
    return ChatOpenAI(
        model=settings.model,
        base_url=settings.base_url.rstrip("/"),
        api_key=api_key,
        temperature=settings.temperature,
        max_completion_tokens=settings.max_output_tokens,
        timeout=settings.timeout_seconds,
        max_retries=settings.max_retries,
    )


def build_embedding_model(settings: EmbeddingSettings) -> EmbeddingModel:
    api_key = settings.api_key.get_secret_value()
    if not api_key:
        raise ValueError(f"Embedding 模型 {settings.model} 未配置 API Key")
    client = OpenAIEmbeddings(
        model=settings.model,
        base_url=settings.base_url.rstrip("/"),
        api_key=api_key,
        dimensions=settings.dimension,
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


@dataclass(frozen=True)
class ModelRegistry:
    rule_extraction: ChatOpenAI
    evidence_classification: ChatOpenAI
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
        bid_chat=build_chat_model(resolved.bid_chat_model),
        evidence_chat=build_chat_model(resolved.evidence_chat_model),
        embedding=build_embedding_model(resolved.embedding_model),
        rerank=RerankModel(resolved.rerank_model),
    )
