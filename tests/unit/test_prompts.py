"""Prompt 配置路径、System/Human 模板和工具说明测试。"""

import pytest

from app.prompts.loader import load_tool_description, render_prompt


def test_evidence_ingestion_prompt_separates_business_relations_from_similarity():
    system = render_prompt(
        "evidence_ingestion",
        current_material="尚无已保存结果",
        history_view="历史资料目录",
    )
    assert "具体业务事项" in system
    assert "仅主体/供方相同" in system
    assert "不是关系枚举" in system
    assert "简短短语" in system and "evidence_summary" in system
    assert "处理完成状态不写入摘要" in system
    assert "补充、证明、更正或有依据的矛盾" in system
    assert "不得丢掉当前材料自身的图片回查入口" in system


def test_evidence_tool_contract_and_parameter_schema_are_separate_from_strategy():
    from app.models.evidence import EvidenceMaterialInput

    description = load_tool_description("save_evidence_material")
    assert "完整覆盖" in description
    assert "material、file_ids" in description
    assert "不另按历史摘要读取预算裁剪" in description
    # 如何认定关系、何时保存、保留哪些看图事实由 System 规定。
    assert "仅主体/供方相同" not in description
    assert "首次整理须保存" not in description
    schema = EvidenceMaterialInput.model_json_schema()
    relations = schema["properties"]["relations"]
    assert "简短关系短语" in relations["description"]
    assert "evidence_summary" in relations["description"]
    assert "仅同一主体不建立关系" not in relations["description"]
    assert "enum" not in str(relations) and "maxLength" not in str(relations)
    assert set(schema["properties"]) == {"bid_summary", "evidence_summary", "relations"}


def test_rule_system_and_input_are_separate():
    system = render_prompt(
        "rule_extraction",
        rule_view="当前暂无规则",
    )
    human = render_prompt(
        "rule_extraction_input",
        file_id="file-1",
        file_name="技术规范.md",
        source_path="规则/技术规范.md",
        history_files="暂无已处理历史文件",
        reading_scope="第 1 批；还有后续内容：否",
        continuation="这是当前文件的首批内容。",
        source_context="技术要求：像素不低于640x480",
    )
    assert "当前暂无规则" in system
    assert "640x480" not in system
    assert "640x480" in human
    assert "file-1" in human


def test_missing_prompt_variable_fails_fast():
    with pytest.raises(KeyError):
        render_prompt("rule_extraction")


def test_audit_report_template_is_a_standalone_static_fragment():
    # 报告模板独立维护，由主 Agent System 引用。
    rendered = render_prompt("audit_report_template")
    assert "${" not in rendered
    assert "# 〈请填写报告标题〉" in rendered
    assert "标题为1～120字" in rendered
    assert "## 总体总结" in rendered
    assert "### 分类总结" not in rendered
    assert "## 重点问题" in rendered
    assert "实际发现：" in rendered
    assert "复核建议：" in rendered
    assert "### 〈rule_id〉" in rendered
    for label in (
        "结论：",
        "审核说明：",
        "招标片段：",
        "投标片段：",
        "资料片段：",
        "图片来源：",
    ):
        assert label in rendered
    assert "AgentAuditDecision" not in rendered


def test_all_tool_descriptions_are_available():
    for name in (
        "request_evidence_review",
        "report_document",
        "search_documents",
        "save_evidence_material",
        "submit_rule_delta",
        "view_image",
        "run_python",
    ):
        assert load_tool_description(name)


def test_audit_system_uses_template_once_and_child_has_no_json_schema():
    template = render_prompt("audit_report_template")
    system = render_prompt(
        "bid_agent", rule_view="规则视图", report_markdown_contract=template
    )
    assert system.count(template) == 1
    assert "AgentAuditDecision" not in system
    assert "规则视图" in system
    child = render_prompt("evidence_subagent")
    assert "EvidenceReviewDecision" not in child
    assert "JSON" in child


def test_three_conclusions_and_conditional_requirements_are_explicit():
    contract = render_prompt("audit_report_template")
    assert "结论：〈符合、不符合、人工复核，选一个〉" in contract
    assert "- 风险：" not in contract
    assert "本项无需响应" in contract and "非联合体" in contract
    assert "适用前提尚不能确认" in contract
    assert "轮数耗尽" in contract and "尚未开展" in contract
    assert "来源填写（按实际使用的依据）" in contract
    assert "实际发现／审核说明" in contract
    assert "完整 chunk_id" in contract and "版本后缀" in contract


def test_audit_strategy_keeps_scope_and_does_not_force_per_round_saves():
    system = render_prompt("bid_agent", rule_view="规则", report_markdown_contract="契约")
    assert "显式选择授权招标文件" in system
    assert "默认范围是当前投标文件" in system
    assert "同主体、对象、版本和来源范围" in system
    assert "不为填写来源ID重新检索" in system
    assert "每轮核验后，只要" not in system
    assert "无需逐条单开保存轮" in system
    assert "解析转写不等于" in system
    child = render_prompt("evidence_subagent")
    assert "完整 chunk_id" in child and "版本后缀" in child
    assert "主 Agent 收到的是你的正文" in child
    assert "招标条款" in child and "实际来自原文或看图的事实" in child


def test_incremental_report_guidance_preserves_dependencies_and_fixed_round_limit():
    prompt = render_prompt(
        "bid_agent", rule_view="规则", report_markdown_contract="模板"
    )
    assert "同轮 tool_calls" in prompt and "不要等全部规则查完" in prompt
    assert "不能预测工具返回" in prompt
    reminder = render_prompt(
        "bid_agent_last_tool_round",
        current_step=7,
        max_steps=8,
        missing="rule-2",
    )
    assert "最后一个工具轮" in reminder and "rule-2" in reminder
    assert "下一轮不绑定任何工具" in reminder
    with pytest.raises(FileNotFoundError):
        render_prompt("bid_agent_progress")
    description = load_tool_description("report_document")
    assert "仍保存草稿" in description and "未命中/不唯一时整批不保存" in description
    assert "先返回“当前草稿状态”" in description
    assert "状态首部与分隔文字不属于草稿" in description
    assert "不返回变更正文" in description


def test_evidence_delegation_separates_criteria_claims_and_verified_facts():
    prompt = render_prompt(
        "bid_agent", rule_view="当前规则", report_markdown_contract="报告契约"
    )
    for heading in (
        "【核验目标】", "【已确定的适用标准】", "【待核声明】",
        "【已有事实与依据】", "【必须返回的事实】",
    ):
        assert prompt.count(heading) == 1
    assert "不增加工具参数" in prompt
    assert "不以规则ID代替标准正文" in prompt
    assert "申报值保留申报性质" in prompt
    assert "明确反证不能被一般承诺" in prompt
    assert "不要求每条规则单独委派" in prompt


def test_evidence_handoff_preserves_message_shape_variables_and_scope():
    from string import Template

    from app.agents.evidence_subagent import build_evidence_messages
    from app.prompts.loader import load_prompt
    from app.tools.bid_tools import EvidenceCheck

    check = EvidenceCheck(
        rule_ids=["rule-internal-only", "rule-also-internal"],
        verification_request="【已确定的适用标准】按给定公式比对。\n【待核声明】${literal}",
        file_ids=["file-a"],
    )
    messages = build_evidence_messages(
        check, ["file-a", "file-b"], "已授权的一层关联", "详细摘要数据", "既有交接内容"
    )
    assert [message.type for message in messages] == ["system", "human"]
    human = messages[1].content
    assert check.verification_request in human  # 任务正文原样插入，不递归解释占位符。
    assert "rule-internal-only" not in human
    assert "rule-also-internal" not in human
    assert "声明为待核事实" not in human
    assert "适用标准、待核声明、已有事实与依据分别列示" in human
    for value in ("file-a", "已授权的一层关联", "详细摘要数据", "既有交接内容"):
        assert value in human
    assert set(Template(load_prompt("evidence_subagent_input")).get_identifiers()) == {
        "verification_request", "file_ids", "relations", "evidence_files", "continuation"
    }
    assert set(EvidenceCheck.model_json_schema()["properties"]) == {
        "rule_ids", "verification_request", "file_ids"
    }


def test_evidence_response_keeps_decisive_values_sources_and_boundaries():
    prompt = render_prompt("evidence_subagent")
    assert "不要求供应商附件再次记载、签认或证明该标准" in prompt
    assert "不自行补造替代公式或额外审批条件" in prompt
    assert "说明应由主审补充的具体项" in prompt
    assert "承接主审已有事实与依据时保留其依据类型" in prompt
    assert "一次交代对象及版本/期间" in prompt
    assert "逐项保留决定性值及相反值" in prompt
    assert "正文开头的支持情况须与后面的事实一致" in prompt
    assert "摘要未提及不等于原材料未提供" in prompt
    assert "完整 chunk_id" in prompt and "版本后缀" in prompt
    assert "主 Agent 收到的是你的正文" in prompt
    assert "不要输出 JSON、content 外壳、状态枚举或来源数组" in prompt


def test_summary_policy_and_sandbox_protocol_are_separate():
    main = render_prompt("bid_agent", rule_view="规则", report_markdown_contract="契约")
    child = render_prompt("evidence_subagent")
    assert "直接判断，无需再次委派" in main
    assert "不是全库详细摘要" in main and "共用事实不等于共用结论" in main
    assert "优先使用本次给出的详细摘要" in child and "不需要片段ID" in child
    assert "仅为定位线索" not in child and "不能只复述摘要" not in child
    for text in (main, child):
        assert "run_python 实际成功结果" in text
        assert "用print" not in text and "status为success" not in text
    final = render_prompt("evidence_subagent_finalize", current_step=3, max_steps=3)
    assert "已无工具" in final and "摘要充分时可据此回答" in final
    assert "待主审处理" in final
    contract = render_prompt("audit_report_template")
    assert "允许规则结果没有任何片段来源行" in contract
    assert "计算结果不登记成文档来源" in contract
    description = load_tool_description("run_python")
    assert "只有code" in description and "仅status与result" in description
    assert "共同前提" not in description and "规则结论" not in description


def test_ingestion_preserves_formula_roles_and_operation_semantics():
    rule = render_prompt("rule_extraction", rule_view="规则")
    material = render_prompt("evidence_ingestion", current_material="无", history_view="历史")
    assert "全部必要条件" in rule and "材料顺序本身不决定效力" in rule
    assert "互斥条件" in rule
    assert "不能决定有争议原文的效力或自行修改招标门槛" in rule
    assert "不用某个例子算出的数字替代适用公式" in rule
    assert "签发角色和证明范围" in material and "原填值" in material
    assert "实际成功结果" in rule and "实际成功结果" in material
    assert "按预算加载" not in material
