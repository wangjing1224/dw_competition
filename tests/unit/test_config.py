"""配置加载测试。"""

import pytest
import yaml
from pydantic import ValidationError

from app.core import config
from app.core.config import get_settings


def test_retrieval_loads_without_inactive_character_budget():
    raw = yaml.safe_load(
        (config.PROJECT_ROOT / "configs/retrieval.yaml").read_text("utf-8")
    )
    assert "max_source_chars" not in str(raw)
    retrieval = get_settings().retrieval
    assert "max_source_chars" not in type(retrieval).model_fields
    assert retrieval.candidate_top_k == 30 and retrieval.final_top_k == 10


def test_cloud_and_commented_local_examples_validate_without_network(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://workspace.test/api/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "example-key")
    monkeypatch.setenv("RERANK_BASE_URL", "https://workspace.test/api/v1")
    monkeypatch.setenv("RERANK_API_KEY", "example-key")
    raw = (config.PROJECT_ROOT / "configs/model.yaml").read_text("utf-8")
    cloud_values = config._interpolate(yaml.safe_load(raw))
    cloud_embedding = config.EmbeddingSettings.model_validate(
        cloud_values["embedding_model"]
    )
    cloud_rerank = config.RerankSettings.model_validate(cloud_values["rerank_model"])
    assert cloud_embedding.platform == cloud_rerank.platform == "bailian"
    local_text = "# embedding_model:" + raw.split("# embedding_model:", 1)[1]
    local = yaml.safe_load(
        "\n".join(line.removeprefix("# ") for line in local_text.splitlines())
    )
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://localhost:8081/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "local")
    local = config._interpolate(local)
    embedding = config.EmbeddingSettings.model_validate(local["embedding_model"])
    rerank = config.RerankSettings.model_validate(local["rerank_model"])
    assert embedding.platform == rerank.platform == "local"
    assert embedding.batch_size == 16 and embedding.model == "text-embedding-v4"
    assert rerank.cache_dir.as_posix() == "models"
    assert not rerank.api_key.get_secret_value()
    # 两个职责分别选择平台，不受对方影响；本地无需云端凭据或下载权重。
    for emb in (embedding, cloud_embedding):
        for rank in (rerank, cloud_rerank):
            values = get_settings().model_dump()
            values.update(embedding_model=emb, rerank_model=rank)
            assert (
                config.AppSettings.model_validate(values).embedding_model.platform
                == emb.platform
            )


def test_every_prompt_file_and_placeholder_is_documented():
    from string import Template

    prompt_root = config.PROJECT_ROOT / "configs/prompts"
    readme = (prompt_root / "README.md").read_text("utf-8")
    for path in (prompt_root / "templates").glob("*.md"):
        assert path.name in readme, path.name
        for name in Template(path.read_text("utf-8")).get_identifiers():
            assert f"`{name}`" in readme or f"${{{name}}}" in readme, (path.name, name)


def test_settings_load_yaml_and_environment(monkeypatch):
    raw = yaml.safe_load(
        (config.PROJECT_ROOT / "configs/model.yaml").read_text("utf-8")
    )
    key_variable = raw["bid_chat_model"]["api_key"].removeprefix("${").removesuffix("}")
    monkeypatch.setenv(key_variable, "test-secret")
    get_settings.cache_clear()

    settings = get_settings()

    # 配置加载器应忠实读取实际网关模型 ID，不把某个平台的大小写写死。
    raw = yaml.safe_load(
        (settings.project_root / "configs/model.yaml").read_text("utf-8")
    )
    assert settings.bid_chat_model.model == raw["bid_chat_model"]["model"]
    assert settings.bid_chat_model.platform == raw["bid_chat_model"]["platform"]
    assert settings.bid_chat_model.api_key.get_secret_value() == "test-secret"
    assert "test-secret" not in repr(settings.bid_chat_model)
    assert settings.storage.database_path.is_absolute()
    assert settings.rerank_model.platform == "bailian"
    assert settings.rerank_model.cache_dir is None
    assert settings.rule_limit == raw["rule_limit"]
    assert config.AppSettings.model_fields["rule_limit"].default == 10_000


@pytest.mark.parametrize(
    "bid_steps,evidence_steps,legacy_value",
    [(8, 3, "99"), (12, 5, "not-an-integer")],
)
def test_audit_steps_read_yaml_not_legacy_environment(
    monkeypatch, bid_steps, evidence_steps, legacy_value
):
    monkeypatch.setenv("BID_AGENT_MAX_STEPS", legacy_value)
    monkeypatch.setenv("EVIDENCE_AGENT_MAX_STEPS", legacy_value)
    original_load = config._load_yaml

    def load_yaml(path):
        value = original_load(path)
        if path.name == "model.yaml":
            value["audit"].update(
                bid_agent_max_steps=bid_steps,
                evidence_agent_max_steps=evidence_steps,
                file_concurrency=4,
                model_concurrency=6,
            )
        return value

    monkeypatch.setattr(config, "_load_yaml", load_yaml)
    audit = get_settings().audit
    assert audit.bid_agent_max_steps == bid_steps
    assert audit.evidence_agent_max_steps == evidence_steps
    assert audit.file_concurrency == 4
    assert audit.model_concurrency == 6


@pytest.mark.parametrize("omit_audit", [False, True])
def test_audit_steps_keep_defaults_when_yaml_omits_them(monkeypatch, omit_audit):
    monkeypatch.setenv("BID_AGENT_MAX_STEPS", "99")
    monkeypatch.setenv("EVIDENCE_AGENT_MAX_STEPS", "99")
    original_load = config._load_yaml

    def load_yaml(path):
        value = original_load(path)
        if path.name == "model.yaml":
            if omit_audit:
                value.pop("audit")
            else:
                value["audit"].pop("bid_agent_max_steps")
                value["audit"].pop("evidence_agent_max_steps")
        return value

    monkeypatch.setattr(config, "_load_yaml", load_yaml)
    audit = get_settings().audit
    assert audit.bid_agent_max_steps == 8
    assert audit.evidence_agent_max_steps == 4


@pytest.mark.parametrize("field", ["bid_agent_max_steps", "evidence_agent_max_steps"])
@pytest.mark.parametrize("value", [0, -1, 1.5, "invalid"])
def test_audit_steps_require_positive_integers(field, value):
    with pytest.raises(ValidationError, match=field):
        config.AuditSettings.model_validate({field: value})


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
    original_load = config._load_yaml

    def load_yaml(path):
        value = original_load(path)
        if path.name == "model.yaml":
            value["parser_vision_model"].update(
                base_url="${PARSER_VISION_MODEL_BASE_URL}",
                api_key="${PARSER_VISION_MODEL_API_KEY}",
                model="${PARSER_VISION_MODEL_NAME}",
            )
            value["bid_chat_model"]["api_key"] = "${BID_CHAT_MODEL_API_KEY}"
            value["parsing"]["vision_enabled"] = False
        return value

    monkeypatch.setattr(config, "_load_yaml", load_yaml)
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


def test_current_six_chat_roles_use_bailian_without_replacing_legacy_keys(monkeypatch):
    from app.llm.model_capabilities import get_model_capabilities

    monkeypatch.setenv("BAILIAN_CHAT_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("BAILIAN_CHAT_API_KEY", "bailian-test-key")
    monkeypatch.setenv("BID_CHAT_MODEL_API_KEY", "legacy-ds-test-key")
    settings = get_settings()
    for role in (
        "rule_extraction_model",
        "evidence_classification_model",
        "bid_summary_model",
        "bid_chat_model",
        "evidence_chat_model",
        "parser_vision_model",
    ):
        model = getattr(settings, role)
        assert model.platform == "bailian"
        assert model.model == "deepseek-v4.1-flash"
        assert model.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        assert model.api_key.get_secret_value() == "bailian-test-key"
        assert not model.enable_thinking
        if model.context_window_tokens is not None:
            caps = get_model_capabilities(model)
            assert caps.max_context_tokens == 1_000_000
            assert caps.max_output_tokens == 393_216
            assert caps.image_tokens_per_image == 1024
    assert settings.parser_vision_model.max_output_tokens == 16384


@pytest.mark.parametrize("platform", ["scnet", "unknown", None])
def test_chat_platform_must_be_explicit_supported_value(platform):
    values = get_settings().rule_extraction_model.model_dump()
    values["platform"] = platform
    with pytest.raises(ValidationError, match="platform"):
        config.ChatModelSettings.model_validate(values)
    values.pop("platform")
    with pytest.raises(ValidationError, match="platform"):
        config.ChatModelSettings.model_validate(values)


def test_rule_budget_configuration_and_legacy_roles():
    settings = get_settings()
    assert settings.rule_extraction_model.context_window_tokens == 1_000_000
    assert settings.rule_extraction_model.max_output_tokens is None
    assert settings.rule_extraction_model.compaction_trigger_ratio == 0.90
    assert settings.rule_extraction_model.tokenizer == "DeepSeek"
    for role in (
        settings.bid_summary_model,
        settings.bid_chat_model,
        settings.evidence_chat_model,
    ):
        assert role.max_output_tokens is None
        assert role.context_window_tokens == 1_000_000


@pytest.mark.parametrize(
    "values",
    [
        {"context_window_tokens": 0},
        {"context_window_tokens": True},
        {"compaction_trigger_ratio": 0},
        {"compaction_trigger_ratio": 1},
        {"max_output_tokens": 8192},
        {"context_window_tokens": None},
    ],
)
def test_invalid_budget_settings_rejected(values):
    data = get_settings().rule_extraction_model.model_dump()
    data.update(values)
    with pytest.raises(ValidationError):
        config.ChatModelSettings.model_validate(data)


def test_unmigrated_roles_cannot_silently_drop_output_limit():
    data = get_settings().model_dump()
    data["parser_vision_model"].update(
        context_window_tokens=10000, max_output_tokens=None
    )
    with pytest.raises(ValidationError, match="尚未接入"):
        config.AppSettings.model_validate(data)
