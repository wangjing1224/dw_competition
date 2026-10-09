"""Prompt 配置路径、System/Human 模板和工具说明测试。"""

import json
import re
from string import Template

import pytest

from app.prompts.loader import load_prompt, load_tool_description, render_prompt


def test_parser_image_prompt_keeps_visible_marks_without_authenticity_judgment():
    from app.parsers.vision import ImageReading

    prompt = render_prompt("parser_image_transcription")
    assert "边角、页脚和水印" in prompt
    assert "样本、示例、AI生成、作废或效力声明" in prompt
    assert "本图实际可见内容" in prompt and "不由文件名、上下文或其他图片补造" in prompt
    assert "标识本身不导致 partial 或 skipped" in prompt
    assert "未见标识不等于已确认非AI生成" in prompt
    assert "保留其余已确认内容及具体读取限制" in prompt
    assert "图中的位置" in prompt
    # F3仅补充转写要求；输出字段、状态集合、JSON转义及表格约束仍用原契约。
    example = json.loads(next(line for line in prompt.splitlines() if line.startswith('{"status":')))
    assert set(example) == set(ImageReading.model_fields) == {"status", "content", "reason"}
    assert example["status"].split("|") == ["readable", "partial", "unreadable", "skipped"]
    assert "rowspan/colspan" in prompt and "单个合法 JSON 对象" in prompt


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
    assert "多个互不冲突的修改优先合并在一次edit中" in system
    assert "解析转写不等于" in system
    child = render_prompt("evidence_subagent")
    assert "完整 chunk_id" in child and "版本后缀" in child
    assert "主 Agent 收到的是你的正文" in child
    assert "招标条款" in child and "实际来自原文或看图的事实" in child


def test_incremental_report_guidance_preserves_dependencies_and_fixed_round_limit():
    prompt = render_prompt(
        "bid_agent", rule_view="规则", report_markdown_contract="模板"
    )
    assert "同次响应的tool_calls" in prompt and "不等待全部事项结束" in prompt
    assert "不能使用同轮尚未返回的B结果写稿" in prompt
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
    assert "原填或申报值保留其身份" in prompt
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
        assert "### run_python" in text and "采用成功执行的实际结果" in text
        assert "用print" not in text and "status为success" not in text
    final = render_prompt("evidence_subagent_finalize", current_step=4, max_steps=4)
    assert "已无工具" in final and "摘要充分时可据此回答" in final
    assert "待主审处理" in final
    contract = render_prompt("audit_report_template")
    assert "允许规则结果没有任何片段来源行" in contract
    assert "计算结果不登记成文档来源" in contract
    description = load_tool_description("run_python")
    assert "只有code" in description and "只含status和result" in description
    assert "共同前提" not in description and "规则结论" not in description


def test_ingestion_preserves_formula_roles_and_operation_semantics():
    rule = render_prompt("rule_extraction", rule_view="规则")
    material = render_prompt("evidence_ingestion", current_material="无", history_view="历史")
    assert "全部必要条件" in rule and "文件顺序本身不决定效力" in rule
    assert "互斥条件" in rule
    assert "不能决定有争议原文的效力或自行修改招标门槛" in rule
    assert "不用某个例子算出的数字替代适用公式" in rule
    assert "签发角色和证明范围" in material and "原填值" in material
    assert "实际成功结果" in rule and "实际成功结果" in material
    assert "按预算加载" not in material


@pytest.mark.parametrize("name,variables,tool_names", [
    ("bid_agent", {"rule_view": "规则", "report_markdown_contract": "模板"},
     ["search_documents", "view_image", "request_evidence_review", "report_document", "run_python"]),
    ("evidence_subagent", {}, ["search_documents", "view_image", "run_python"]),
    ("rule_extraction", {"rule_view": "规则"},
     ["search_documents", "view_image", "submit_rule_delta", "run_python"]),
    ("evidence_ingestion", {"current_material": "当前", "history_view": "历史"},
     ["search_documents", "view_image", "save_evidence_material", "run_python"]),
])
def test_tool_norms_only_list_role_tools_once(name, variables, tool_names):
    import re

    system = render_prompt(name, **variables)
    assert system.count("## 工具使用规范") == 1
    assert re.findall(r"^### (.+)$", system, re.M) == ["通用规则", *tool_names]
    assert "同名工具多次调用" in system and "系统顺序执行" in system
    assert "不以打印预设答案冒充验证" in system
    assert "不同调用不共享变量" in system
    assert "4.2" not in system and "${" not in system
    for name in tool_names:
        assert load_tool_description(name)


def test_task35_strategy_scope_facts_and_templates():
    main = render_prompt("bid_agent", rule_view="规则", report_markdown_contract="模板")
    assert "不赋予你直接读取资料图片的权限" in main
    assert "全部checks、起始ID和目标" in main
    assert "不为重复确认或补来源ID再次委派" in main
    assert "不删掉来源后仍保留冒称已核" in main
    assert "报告结论数量由代码统计" in main
    assert "明细已收束后" in main and "不固定各阶段占几轮" in main
    assert "较弱下界不能当成精确实际值" in main
    assert "即使另一理由已经足以判不符合" in main
    child = render_prompt("evidence_subagent")
    assert "完整来源键紧邻该事实" in child
    assert "混合依据分别标明" in child
    assert "主审交接事实保留交接身份" in child
    rule = render_prompt("rule_extraction", rule_view="规则")
    assert "不承担独立评分" in rule and "不因位于评分章节一并排除" in rule
    assert "表格、清单、附件" in rule and "条件本身已明确而对象绑定尚未确认" in rule
    evidence = render_prompt("evidence_ingestion", current_material="无", history_view="历史")
    for text in ("总数一致", "未注明", "部分对应", "逐图保留", "同一产品", "不为减少联查"):
        assert text in evidence
    delegated = load_tool_description("request_evidence_review")
    assert "任一check身份非法则本批均未启动" in delegated
    assert "其他check可能已执行" in delegated
    assert "全部checks、起始ID和目标" not in delegated  # 恢复策略留在System。


def test_report_tool_edit_fewshot_is_valid_and_not_in_template():
    import json

    description = load_tool_description("report_document")
    example = next(line for line in description.splitlines() if line.startswith('{"action":"edit"'))
    edits = json.loads(example)
    current = "## 总体总结\n\n待填写。\n\n## 重点问题\n\n待填写。"
    for edit in edits["edits"]:
        assert current.count(edit["old_text"]) == 1
        current = current.replace(edit["old_text"], edit["new_text"])
    assert "待填写" not in current
    assert current.count("## 总体总结") == current.count("## 重点问题") == 1
    template = render_prompt("audit_report_template")
    assert example not in template
    assert "采用摘要身份，不补造片段行" in template


@pytest.mark.parametrize("name,count", [
    ("save_evidence_material", 3), ("report_document", 3),
    ("request_evidence_review", 1), ("run_python", 2),
])
def test_every_documented_call_uses_the_exposed_tool_schema(
    name, count, temp_settings, tmp_path
):
    import asyncio
    from types import SimpleNamespace

    from app.audit.sources import SourceLedger
    from app.reports.markdown_report import ReportDocument
    from app.tools.bid_tools import build_evidence_review_tool
    from app.tools.common_tools import build_python_tool
    from app.tools.evidence_ingestion_tools import EvidenceSession, build_evidence_ingestion_tools
    from app.tools.report_tools import report_tool
    from tests.helpers.audit import SequenceModel, audit_context

    context = audit_context()
    ledger = SourceLedger(context.parsed_documents)
    session = EvidenceSession(
        library=context.evidence_library, current=context.parsed_documents["e-3"],
        history={key: context.parsed_documents[key] for key in ("e-1", "e-2")},
        settings=temp_settings, counter=None,
    )
    tools = [
        *build_evidence_ingestion_tools(session),
        report_tool(ReportDocument(tmp_path / "draft.md", context.rule_set, ledger)),
        build_evidence_review_tool(
            context=context, settings=temp_settings, model=SequenceModel([]),
            retrieval=SimpleNamespace(), ledger=ledger, limiter=asyncio.Semaphore(1),
            trace_context={}, records=[],
        ),
        build_python_tool(temp_settings.python_tool),
    ]
    tool = next(tool for tool in tools if tool.name == name)
    samples = [json.loads(line) for line in tool.description.splitlines() if line.startswith("{")]
    calls = [sample for sample in samples if "status" not in sample]
    assert len(calls) == count
    schema = tool.tool_call_schema.model_json_schema()
    assert "tool_call_id" not in schema["properties"]
    for payload in calls:
        assert set(payload) == set(schema["properties"])
        tool.tool_call_schema.model_validate(payload)
        injected = {"tool_call_id": "sample-call"} if "tool_call_id" in tool.args_schema.model_fields else {}
        tool.args_schema.model_validate({**payload, **injected})


@pytest.mark.parametrize("name,variables,headings", [
    ("rule_extraction", {"rule_view"}, ["准入与粒度", "分类和等级", "工具使用规范", "依据与项目简介", "阅读与结束"]),
    ("bid_agent", {"rule_view", "report_markdown_contract"}, [
        "职责与边界", "工具使用规范", "事实采用与核验边界", "负面理由与适用性",
        "资料核验交接", "依据与来源保存", "成果与收尾", "本次完整规则（业务数据）",
    ]),
    ("evidence_subagent", set(), ["工具使用规范", "事实与支持范围", "核验返回"]),
    ("audit_report_template", set(), [
        "内容要求", "内部格式", "事实、判断与建议", "重点问题与处理建议",
        "来源填写（按实际使用的依据）", "草稿与完成",
    ]),
])
def test_task36_keeps_template_variables_and_section_structure(name, variables, headings):
    source = load_prompt(name)
    assert set(Template(source).get_identifiers()) == variables
    # 示例围栏中的标题是正文示例，不作为System章节重复计数。
    outside_examples = re.sub(r"^```[^\n]*\n.*?^```\s*$", "", source, flags=re.M | re.S)
    assert re.findall(r"^## (.+)$", outside_examples, re.M) == headings
    rendered = render_prompt(name, **{key: f"输入:{key}" for key in variables})
    assert "${" not in rendered
    assert all(rendered.count(f"输入:{key}") == 1 for key in variables)


def test_task36_rule_coverage_replaces_general_guidance_without_changing_admission():
    prompt = render_prompt("rule_extraction", rule_view="规则")
    assert "覆盖以已保存requirement中的实际条件为准" in prompt
    assert "项目简介或reason提过" in prompt
    assert "例外与触发条件属于规则的一部分" in prompt
    assert "仅简介变化且无未保存的有效要求时可只更新简介" in prompt
    assert "背景、纯评分或范围外事项按准入边界处理" in prompt
    assert "已确认无变化的主题沿用，不每轮重新扫描全库" in prompt
    assert "不按每个单元格机械拆分" in prompt
    assert "应replace为更正后的适用条款" in prompt
    assert "效力未知的冲突说明双方依据" in prompt
    assert "提取可由投标内容或证明材料核验的独立要求，保留适用对象" not in prompt


def test_task36_main_child_handoff_and_evidence_boundaries_are_synchronized():
    # 仅验证装载和保护提示词边界，不模拟或声称模型已给出正确业务判断。
    main = render_prompt("bid_agent", rule_view="规则", report_markdown_contract="契约")
    child = render_prompt("evidence_subagent")
    common = "采用或交接派生事实时，说明对象/版本、单位、输入范围、已做的去重/替换/排除、最终范围及实际成功结果。"
    for prompt in (main, child):
        assert prompt.count(common) == 1
        assert "数量相同也不自动证明成员相同" in prompt
        assert "不能从数量2再扣一次B" in prompt
        assert "不用汇总值补造成员" in prompt
        assert "确定派生事实前，明确输入对象" not in prompt
    assert "同一checks内的子会话不共享新结果" in main
    assert "将事实与来源组织成同一次明细保存" in main
    assert "不因主审未亲自读取而漏填，也不声称主审亲自看图" in main
    assert "未完成的必要处理说明具体输入或执行缺口" in child
    assert "不自动证明真实主体不具资格" in main
    assert "不推导真实主体没有该资质" in child
    assert "不能把测试/展示副本的处理归责于投标人" in main
    assert "子返回的“未取得支持”不是“已反证”" in main
    assert "存在明确反证时也一并返回" in child
    assert "未受影响的充分事实仍可采用" in main
    assert "相关核验尚未进行或仅因工具失败未完成，则保留待填写" in main
    assert "未完成的读取或工具失败另说明执行缺口" in child
    assert "不能用“人工复核”填满尚未核验的块" in main
    assert "整条规则由主审判断" in child
    template = render_prompt("audit_report_template")
    assert "无法证明不等于已证明违反，也不等于已符合" in template
    assert "未受这些限制影响的明确反证仍按规则判断" in template
    assert "互换依据类型仍按事实判断" in template


@pytest.mark.parametrize("example_index", [0, 1])
def test_complete_report_examples_use_existing_parser_and_real_source_keys(example_index):
    from types import SimpleNamespace

    from app.audit.sources import SourceLedger
    from app.reports.markdown_report import parse_report
    from tests.helpers.audit import audit_context, filled_report

    template = render_prompt("audit_report_template")
    examples = [block for block in re.findall(r"^```markdown\n(.*?)^```", template, re.M | re.S) if block.startswith("### 〈本项真实rule_id〉")]
    assert len(examples) == 2
    context = audit_context()
    context.rule_set.rules[0].requirement = "检验记录须填写本批批次及样品编号。"
    parsed = context.parsed_documents["e-1"]
    parsed.document.file_name = "检验记录"
    ledger = SourceLedger(context.parsed_documents)
    key = "evidence-chunk-v1"
    if example_index == 0:
        ledger.chunk_read(SimpleNamespace(
            source=SimpleNamespace(file_id="e-1", section="检验记录完整相关表"),
            parse_version=parsed.markdown_path, page_numbers=[], chunk_id=key,
            content="本批次B01，样品编号栏未填写；实际提交相关字段未作脱敏或删节。",
        ))
    block = examples[example_index].replace("〈本项真实rule_id〉", "rule-1")
    block = block.replace("〈随上述事实返回的完整真实chunk_id〉", key)
    text = filled_report(context.rule_set, version=2).split("### rule-1", 1)[0] + block
    decision, missing = parse_report(text, context.rule_set, ledger, version=2, final=True)
    assert not missing
    rule = decision.categories[0].rules[0]
    assert rule.result == ["non_compliant", "compliant"][example_index]
    assert rule.summary.evidence_chunk_ids == ([key] if example_index == 0 else [])
    if example_index == 0:
        for invalid in (text.replace(key, "unknown-chunk"), text.replace("资料片段：", "投标片段：")):
            with pytest.raises(ValueError, match="未实际读取、类型不符或越权"):
                parse_report(invalid, context.rule_set, ledger, version=2, final=True)
    else:
        assert not ledger.sources and "摘要记载" in rule.summary.observation
