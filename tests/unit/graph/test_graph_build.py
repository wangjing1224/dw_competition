"""使用占位凭据验证主/子 LangGraph 和五个 BaseTool 可正常编译。"""

from pydantic import SecretStr

from app.graph.audit_task_graph import build_audit_graph
from app.llm.factory import build_model_registry


def test_graph_compiles_without_calling_external_models(temp_settings):
    def with_key(value):
        return value.model_copy(update={"api_key": SecretStr("test-key")})

    settings = temp_settings.model_copy(
        update={
            "rule_extraction_model": with_key(
                temp_settings.rule_extraction_model
            ),
            "evidence_classification_model": with_key(
                temp_settings.evidence_classification_model
            ),
            "bid_chat_model": with_key(temp_settings.bid_chat_model),
            "evidence_chat_model": with_key(temp_settings.evidence_chat_model),
            "embedding_model": with_key(temp_settings.embedding_model),
        }
    )

    graph = build_audit_graph(settings, build_model_registry(settings))

    assert graph is not None
