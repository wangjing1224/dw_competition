"""模型工厂参数契约测试。"""

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.llm import factory


def test_budgeted_model_has_no_fixed_output_cap_and_uses_call_allowance(
    monkeypatch, temp_settings
):
    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(factory, "ChatOpenAI", FakeChatOpenAI)
    settings = temp_settings.rule_extraction_model.model_copy(
        update={"api_key": SecretStr("test-key")}
    )
    factory.build_chat_model(settings)
    assert "max_tokens" not in captured["extra_body"]
    real = ChatOpenAI(**captured)
    options = factory.budgeted_generation_options(settings, 9000)
    payload = real._get_request_payload([HumanMessage(content="测试")], **options)
    assert payload["extra_body"] == {
        "max_tokens": 9000,
        "enable_thinking": settings.enable_thinking,
    }
    assert payload.get("max_completion_tokens") is None


def test_chat_model_uses_scnet_generation_parameters(monkeypatch, temp_settings):
    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(factory, "ChatOpenAI", FakeChatOpenAI)
    settings = temp_settings.evidence_classification_model.model_copy(
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
