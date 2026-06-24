"""模型工厂参数契约测试。"""

from pydantic import SecretStr

from app.llm import factory


def test_chat_model_uses_scnet_generation_parameters(monkeypatch, temp_settings):
    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(factory, "ChatOpenAI", FakeChatOpenAI)
    settings = temp_settings.rule_extraction_model.model_copy(
        update={"api_key": SecretStr("test-key")}
    )

    factory.build_chat_model(settings)

    assert captured["extra_body"] == {
        "max_tokens": settings.max_output_tokens,
        "enable_thinking": False,
    }


def test_llama_cpp_embedding_does_not_send_dimensions(
    monkeypatch,
    temp_settings,
):
    """llama.cpp 不支持 dimensions，维度只在本地用于响应校验。"""

    captured = {}

    class FakeOpenAIEmbeddings:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(factory, "OpenAIEmbeddings", FakeOpenAIEmbeddings)

    model = factory.build_embedding_model(temp_settings.embedding_model)

    assert "dimensions" not in captured
    assert model.dimension == 1024
