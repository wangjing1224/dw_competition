"""从 YAML、.env 和环境变量加载并校验应用配置。"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATTERN = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


class ConfigModel(BaseModel):
    """所有配置模型的严格基类，防止 YAML 拼错字段后被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


class ChatModelSettings(ConfigModel):
    platform: Literal["deepseek", "bailian", "openai"]
    """单个 ChatModel 职责对应的完整连接和生成参数。"""

    base_url: str
    api_key: SecretStr
    model: str
    temperature: float = Field(ge=0)
    timeout_seconds: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    # 尚未迁移的调用保留固定输出额度；接入控制器的调用逐请求推导。
    max_output_tokens: int | None = Field(default=None, gt=0)
    context_window_tokens: int | None = Field(default=None, gt=0, strict=True)
    compaction_trigger_ratio: float = Field(default=0.90, gt=0, lt=1)
    tokenizer: str = "DeepSeek"
    enable_thinking: bool = False
    structured_output_mode: Literal["json_schema", "json_object", "prompt_only"]

    @model_validator(mode="after")
    def validate_budget_mode(self):
        if (self.max_output_tokens is None) == (self.context_window_tokens is None):
            raise ValueError(
                "必须二选一：固定 max_output_tokens 或 context_window_tokens"
            )
        return self


class EmbeddingSettings(ConfigModel):
    """Embedding 服务配置；dimension 必须与服务实际输出一致。"""

    platform: Literal["local", "bailian"]
    base_url: str
    api_key: SecretStr
    model: str
    dimension: int = Field(gt=0)
    batch_size: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    max_concurrency: int = Field(default=2, ge=1)

    @model_validator(mode="after")
    def validate_platform(self):
        if self.platform == "bailian":
            from app.llm.bailian_retrieval import EMBEDDING_MODELS

            limits = EMBEDDING_MODELS.get(self.model)
            if limits is None:
                raise ValueError(f"尚未登记百炼Embedding型号：{self.model}")
            if self.dimension not in limits.dimensions:
                raise ValueError(f"{self.model} 不支持维度 {self.dimension}")
            if self.batch_size > limits.max_items:
                raise ValueError(f"{self.model} 每批最多 {limits.max_items} 条")
        return self


class RerankSettings(ConfigModel):
    """按平台校验；本地加载权重，百炼通过原生 HTTP 重排。"""

    platform: Literal["local", "bailian"]
    model: str
    base_url: str = ""
    api_key: SecretStr = SecretStr("")
    timeout_seconds: float = Field(default=120, gt=0)
    max_retries: int = Field(default=2, ge=0)
    max_concurrency: int = Field(default=2, ge=1)
    source: Literal["modelscope", "local"] = "modelscope"
    cache_dir: Path | None = None
    device: str = "auto"
    dtype: Literal["auto", "float16", "bfloat16", "float32"] = "auto"
    batch_size: int | None = Field(default=None, gt=0)
    max_length: int | None = Field(default=None, gt=0)
    top_n: int = Field(gt=0)
    instruction: str
    local_files_only: bool = False
    allow_degradation: bool | None = None

    @model_validator(mode="after")
    def validate_platform(self):
        if self.platform == "local":
            if any(
                value is None
                for value in (self.cache_dir, self.batch_size, self.max_length)
            ):
                raise ValueError("本地Rerank必填 cache_dir、batch_size、max_length")
        elif self.model != "qwen3.7-text-rerank":
            raise ValueError("百炼Rerank当前仅适配 qwen3.7-text-rerank 原生协议")
        if self.allow_degradation is None:
            self.allow_degradation = self.platform == "local"
        return self


class RetrievalSettings(ConfigModel):
    """本地分段、初召回、融合和最终回查数量配置。"""

    chunk_tokens: int = Field(gt=0)
    chunk_overlap: int = Field(ge=0)
    candidate_top_k: int = Field(gt=0)
    final_top_k: int = Field(gt=0)
    semantic_weight: float = Field(ge=0)
    keyword_weight: float = Field(ge=0)
    # Task32 D1：暂时停用返回字符预算；恢复须同时恢复检索与工具层裁剪。
    # max_source_chars: int = Field(gt=0)


class StorageSettings(ConfigModel):
    """SQLite、本地文件和 ZIP 安全限制配置。"""

    data_root: Path
    database_path: Path
    max_upload_bytes: int = Field(gt=0)
    max_zip_entry_bytes: int = Field(gt=0)
    max_zip_total_bytes: int = Field(gt=0)


class RuntimeSettings(ConfigModel):
    """不影响业务结果的可选运行时集成配置。"""

    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "dw-competition"


class AuditSettings(ConfigModel):
    """主/子审核 Agent 的有界循环。"""

    bid_agent_max_steps: int = Field(default=8, gt=0)
    evidence_agent_max_steps: int = Field(default=3, gt=0)
    file_concurrency: int = Field(default=2, ge=1)
    model_concurrency: int = Field(default=3, ge=1)


class ParsingSettings(ConfigModel):
    """只控制解析资源，不承担 Agent Token 预算。视觉调用须显式启用。"""

    vision_enabled: bool = False
    max_image_bytes: int = Field(default=20_000_000, gt=0)
    max_image_pixels: int = Field(default=40_000_000, gt=0)
    vision_image_max_side: int = Field(default=2048, ge=256, le=8192)
    pdf_render_dpi: int = Field(default=120, ge=72, le=300)
    pdf_render_max_dpi: int = Field(default=300, ge=72, le=600)
    max_pdf_pages: int = Field(default=1000, gt=0)
    max_sheet_cells: int = Field(default=500_000, gt=0)
    max_embedded_depth: int = Field(default=8, gt=0)
    max_embedded_files: int = Field(default=100, gt=0)
    max_embedded_bytes: int = Field(default=100_000_000, gt=0)


class AppSettings(ConfigModel):
    """应用启动后使用的完整已解析配置。"""

    app_name: str = "电力智能招标审核系统"
    debug: bool = False
    project_root: Path = PROJECT_ROOT
    rule_extraction_model: ChatModelSettings
    rule_agent_max_steps: int = Field(default=8, ge=2)
    rule_limit: int = Field(
        default=10_000,
        gt=0,
        strict=True,
        description="规则库总条数上限；超过时调用预留处理函数，当前只警告并继续保存。",
    )
    evidence_classification_model: ChatModelSettings
    evidence_ingestion_max_steps: int = Field(default=8, ge=2)
    bid_summary_model: ChatModelSettings
    bid_chat_model: ChatModelSettings
    evidence_chat_model: ChatModelSettings
    parser_vision_model: ChatModelSettings | None = None
    parsing: ParsingSettings = Field(default_factory=ParsingSettings)
    embedding_model: EmbeddingSettings
    rerank_model: RerankSettings
    retrieval: RetrievalSettings
    storage: StorageSettings
    runtime: RuntimeSettings
    audit: AuditSettings = Field(default_factory=AuditSettings)

    @model_validator(mode="after")
    def validate_budget_adoption(self):
        if self.rule_extraction_model.context_window_tokens is None:
            raise ValueError("规则模型已接入输入控制器，需要 context_window_tokens")
        if self.evidence_classification_model.context_window_tokens is None:
            raise ValueError("资料入库模型已接入输入控制器，需要 context_window_tokens")
        for name in ("bid_summary_model", "bid_chat_model", "evidence_chat_model"):
            model = getattr(self, name)
            if model.context_window_tokens is None:
                raise ValueError(f"{name} 已接入输入控制器，需要 context_window_tokens")
        if (
            self.parser_vision_model
            and self.parser_vision_model.context_window_tokens is not None
        ):
            raise ValueError(
                "parser_vision_model 尚未接入输入控制器，暂保留 max_output_tokens"
            )
        return self

    def ensure_runtime_directories(self) -> None:
        """按需创建运行目录，不使用 .gitkeep 提前占位。"""

        for relative in ("uploads", "parsed", "indexes", "outputs"):
            (self.storage.data_root / relative).mkdir(parents=True, exist_ok=True)
        self.storage.database_path.parent.mkdir(parents=True, exist_ok=True)


def _load_yaml(path: Path) -> dict[str, Any]:
    """读取一个 YAML 对象，并拒绝数组等错误根节点。"""

    with path.open(encoding="utf-8") as file:
        value = yaml.safe_load(file) or {}
    if not isinstance(value, dict):
        raise ValueError(f"配置文件根节点必须是对象: {path}")
    return value


def _interpolate(value: Any) -> Any:
    """递归将 YAML 中完整的 ${ENV_NAME} 字符串替换为环境变量。"""

    if isinstance(value, dict):
        return {key: _interpolate(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_interpolate(item) for item in value]
    if isinstance(value, str):
        matched = ENV_PATTERN.fullmatch(value)
        if matched:
            return os.getenv(matched.group(1), "")
    return value


def _resolve_path(value: str | Path) -> Path:
    """把配置中的相对路径固定解析到项目根目录。"""

    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """加载 .env 和三份 YAML，完成环境变量替换及 Pydantic 校验。"""

    # override=False 保证 shell/部署环境显式设置的变量优先于本地 .env。
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    model_config = _load_yaml(PROJECT_ROOT / "configs/model.yaml")
    retrieval_config = _load_yaml(PROJECT_ROOT / "configs/retrieval.yaml")
    storage_config = _load_yaml(PROJECT_ROOT / "configs/storage.yaml")
    # YAML 负责结构和非敏感参数；.env 只提供 URL、Key 等环境差异。
    raw = _interpolate(
        {
            "app_name": os.getenv("APP_NAME", "电力智能招标审核系统"),
            "debug": os.getenv("APP_DEBUG", "false").lower() == "true",
            **model_config,
            "retrieval": retrieval_config,
            "storage": storage_config,
            "runtime": {
                "langsmith_tracing": os.getenv("LANGSMITH_TRACING", "false").lower()
                == "true",
                "langsmith_api_key": os.getenv("LANGSMITH_API_KEY") or None,
                "langsmith_project": os.getenv("LANGSMITH_PROJECT", "dw-competition"),
            },
        }
    )
    # 数据路径统一转为绝对路径，后端从任意工作目录启动时行为一致。
    raw["storage"]["data_root"] = _resolve_path(raw["storage"]["data_root"])
    raw["storage"]["database_path"] = _resolve_path(raw["storage"]["database_path"])
    # 模型缓存也固定到项目根目录，避免依赖可能被系统清理的用户缓存目录。
    if raw["rerank_model"].get("cache_dir") is not None:
        raw["rerank_model"]["cache_dir"] = _resolve_path(
            raw["rerank_model"]["cache_dir"]
        )
    return AppSettings.model_validate(raw)
