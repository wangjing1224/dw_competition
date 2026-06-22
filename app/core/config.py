"""从 YAML、.env 和环境变量加载并校验应用配置。"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATTERN = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


class ConfigModel(BaseModel):
    """所有配置模型的严格基类，防止 YAML 拼错字段后被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


class ChatModelSettings(ConfigModel):
    """单个 ChatModel 职责对应的完整连接和生成参数。"""

    base_url: str
    api_key: SecretStr
    model: str
    temperature: float = Field(ge=0)
    timeout_seconds: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    max_output_tokens: int = Field(gt=0)
    structured_output_mode: Literal["json_schema", "json_object", "prompt_only"]


class EmbeddingSettings(ConfigModel):
    """Embedding 服务配置；dimension 必须与服务实际输出一致。"""

    base_url: str
    api_key: SecretStr
    model: str
    dimension: int = Field(gt=0)
    batch_size: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    max_retries: int = Field(ge=0)


class RerankSettings(ConfigModel):
    """Rerank 原生服务配置和不可用时的降级策略。"""

    base_url: str
    api_key: SecretStr
    model: str
    top_n: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    allow_degradation: bool = True


class RetrievalSettings(ConfigModel):
    """本地分段、初召回、融合和最终回查长度配置。"""

    chunk_tokens: int = Field(gt=0)
    chunk_overlap: int = Field(ge=0)
    candidate_top_k: int = Field(gt=0)
    final_top_k: int = Field(gt=0)
    semantic_weight: float = Field(ge=0)
    keyword_weight: float = Field(ge=0)
    max_source_chars: int = Field(gt=0)


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


class AppSettings(ConfigModel):
    """应用启动后使用的完整已解析配置。"""

    app_name: str = "电力智能招标审核系统"
    debug: bool = False
    project_root: Path = PROJECT_ROOT
    rule_extraction_model: ChatModelSettings
    evidence_classification_model: ChatModelSettings
    bid_chat_model: ChatModelSettings
    evidence_chat_model: ChatModelSettings
    embedding_model: EmbeddingSettings
    rerank_model: RerankSettings
    retrieval: RetrievalSettings
    storage: StorageSettings
    runtime: RuntimeSettings

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
                "langsmith_project": os.getenv(
                    "LANGSMITH_PROJECT", "dw-competition"
                ),
            },
        }
    )
    # 数据路径统一转为绝对路径，后端从任意工作目录启动时行为一致。
    raw["storage"]["data_root"] = _resolve_path(raw["storage"]["data_root"])
    raw["storage"]["database_path"] = _resolve_path(
        raw["storage"]["database_path"]
    )
    return AppSettings.model_validate(raw)
