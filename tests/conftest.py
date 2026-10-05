"""公共测试配置和临时运行目录 fixture。"""

from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("APP_DEBUG", "false")
    # 单元和集成测试使用 Mock 模型，不应把测试 Trace 上报到开发者的
    # LangSmith 项目；真实模型 E2E 可在独立进程中显式重新开启。
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def temp_settings(tmp_path: Path):
    """隔离数据目录与 Mock 模型协议，不让本地部署切换改变测试基线。"""

    settings = get_settings()
    mock_models = {
        role: getattr(settings, role).model_copy(
            update={
                "platform": "deepseek",
                "base_url": "https://api.deepseek.com",
                "api_key": SecretStr("test-key"),
                "model": "deepseek-v4-flash",
            }
        )
        for role in (
            "rule_extraction_model",
            "evidence_classification_model",
            "bid_summary_model",
            "bid_chat_model",
            "evidence_chat_model",
            "parser_vision_model",
        )
    }
    return settings.model_copy(
        update={
            **mock_models,
            "storage": settings.storage.model_copy(
                update={
                    "data_root": tmp_path / "data",
                    "database_path": tmp_path / "data" / "app.db",
                }
            )
        }
    )
