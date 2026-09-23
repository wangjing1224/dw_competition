"""配置加载测试。"""

import pytest
import yaml
from pydantic import ValidationError

from app.core import config
from app.core.config import get_settings


def test_settings_load_yaml_and_environment(monkeypatch):
    monkeypatch.setenv("BID_CHAT_MODEL_API_KEY", "test-secret")
    get_settings.cache_clear()

    settings = get_settings()

    # 配置加载器应忠实读取实际网关模型 ID，不把某个平台的大小写写死。
    raw = yaml.safe_load(
        (settings.project_root / "configs/model.yaml").read_text("utf-8")
    )
    assert settings.bid_chat_model.model == raw["bid_chat_model"]["model"]
    assert settings.bid_chat_model.api_key.get_secret_value() == "test-secret"
    assert "test-secret" not in repr(settings.bid_chat_model)
    assert settings.storage.database_path.is_absolute()
    assert settings.rerank_model.cache_dir == settings.project_root / "models"
    assert settings.rule_limit == raw["rule_limit"]
    assert config.AppSettings.model_fields["rule_limit"].default == 10_000


@pytest.mark.parametrize("limit", [30, 10_000, 100_000])
def test_rule_limit_reads_yaml(monkeypatch, limit):
    original_load = config._load_yaml

    def load_yaml(path):
        value = original_load(path)
        if path.name == "model.yaml":
            value["rule_limit"] = limit
        return value

    monkeypatch.setattr(config, "_load_yaml", load_yaml)
    assert get_settings().rule_limit == limit


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
def test_rule_limit_requires_positive_integer(limit):
    values = get_settings().model_dump()
    values["rule_limit"] = limit
    with pytest.raises(ValidationError, match="rule_limit"):
        config.AppSettings.model_validate(values)


def test_parser_vision_has_independent_environment(monkeypatch):
    monkeypatch.setenv("PARSER_VISION_MODEL_BASE_URL", "https://vision.example/v1")
    monkeypatch.setenv("PARSER_VISION_MODEL_API_KEY", "vision-secret")
    monkeypatch.setenv("PARSER_VISION_MODEL_NAME", "gateway-vision-id")
    monkeypatch.setenv("BID_CHAT_MODEL_API_KEY", "agent-secret")
    settings = get_settings()
    assert settings.parser_vision_model.base_url == "https://vision.example/v1"
    assert settings.parser_vision_model.api_key.get_secret_value() == "vision-secret"
    assert settings.parser_vision_model.model == "gateway-vision-id"
    assert settings.bid_chat_model.api_key.get_secret_value() == "agent-secret"
    assert not settings.parsing.vision_enabled
    assert "vision-secret" not in repr(settings)
