"""配置加载测试。"""

from app.core.config import get_settings


def test_settings_load_yaml_and_environment(monkeypatch):
    monkeypatch.setenv("BID_CHAT_MODEL_API_KEY", "test-secret")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.bid_chat_model.model == "qwen-plus"
    assert settings.bid_chat_model.api_key.get_secret_value() == "test-secret"
    assert "test-secret" not in repr(settings.bid_chat_model)
    assert settings.storage.database_path.is_absolute()
