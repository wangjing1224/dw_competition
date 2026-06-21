# 任务 12：主审核智能体与 LangGraph

## 1. 目标

实现一次 `AuditTask` 的 LangGraph 主图、`bid_agent` ReAct、确定性 State 更新、完成条件路由和最终 AuditResult 汇总。

## 2. 建议文件

```text
app/agents/bid_agent.py
app/graph/audit_state.py
app/graph/audit_task_graph.py
app/graph/routing.py
app/graph/state_updates.py
app/prompts/loader.py
app/prompts/templates/bid_agent.md
```

### 2.1 文件职责与伪代码归属

| 文件 | 职责 | 对应伪代码 |
|---|---|---|
| `app/graph/audit_state.py` | 定义 `AuditState`、初始状态和当前游标辅助读取 | 第 3 节、5.2 |
| `app/graph/audit_task_graph.py` | 创建、连接并编译主 StateGraph | 5.1 |
| `app/agents/bid_agent.py` | 主 Agent 模型节点和当前工作单元消息构造 | 5.3 |
| `app/graph/state_updates.py` | 校验 Decision、写入结果、推进游标、汇总 AuditResult | 5.4、5.7 |
| `app/graph/routing.py` | Agent 工具路由、规则冲突路由和 `check_completion` | 5.5、5.6 |
| `app/prompts/templates/bid_agent.md` | 主 Agent 固定系统提示词和工具使用约束 | 第 6 节及 5.3 |
| `app/prompts/loader.py` | 读取主 Agent Prompt | 5.3 |
| `tests/unit/graph/test_state_updates.py` | 重复写入、游标推进和文件汇总测试 | 第 8 节 |
| `tests/unit/graph/test_routing.py` | 完成条件、规则覆盖和异常降级测试 | 第 8 节 |
| `tests/integration/test_audit_graph.py` | 主/子 Agent 和工具的图级集成测试 | 第 8 节 |

## 3. AuditState

```python
class AuditState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    task_id: str
    project_id: str
    bidder_id: str
    rule_set: RuleSet
    bid_files: list[DocumentRef]
    current_bid_index: int
    current_rule_index: int
    project_matches: dict[str, ProjectMatch]
    evidence_results: list[EvidenceReviewResult]
    findings: list[Finding]
    file_results: list[FileAuditResult]
    audit_result: AuditResult | None
    errors: list[TaskError]
    react_step_count: int
```

## 4. 主图

```text
AuditTask Context
  -> bid_agent ReAct
  -> 普通工具或 request_evidence_review
  -> BidAgentDecision
  -> apply_bid_agent_output
  -> check_completion
  -> 未完成：继续 bid_agent
  -> 完成：AuditResult
```

## 5. 核心伪代码

### 5.1 图构建

```python
def build_audit_graph():
    graph = StateGraph(AuditState)
    bid_tools = [
        SearchBidResponseTool(
            task_repository=task_repository,
            retrieval_service=retrieval_service,
        ),
        RetrieveRuleSourceTool(source_store=source_store),
        RequestEvidenceReviewTool(evidence_graph=evidence_graph),
    ]
    agent_node = partial(
        bid_agent_node,
        tools=bid_tools,
    )

    graph.add_node("bid_agent", agent_node)
    graph.add_node("tools", ToolNode(bid_tools))
    graph.add_node("apply_output", apply_bid_agent_output_node)
    graph.add_node("build_result", build_audit_result_node)

    graph.set_entry_point("bid_agent")
    graph.add_conditional_edges(
        "bid_agent",
        route_bid_agent,
        {
            "tools": "tools",
            "apply_output": "apply_output",
            "limit_reached": "apply_output",
        },
    )
    graph.add_edge("tools", "bid_agent")
    graph.add_conditional_edges(
        "apply_output",
        route_after_state_update,
        {
            "continue": "bid_agent",
            "complete": "build_result",
        },
    )
    graph.add_edge("build_result", END)
    # 当前版本不做重启续跑，只使用进程内 checkpointer 支持同一任务执行。
    return graph.compile(checkpointer=InMemorySaver())


async def run_audit_graph(task_context):
    graph = build_audit_graph()
    final_state = await graph.ainvoke(
        build_initial_audit_state(task_context),
        config={
            "configurable": {
                "thread_id": task_context.task_id,
            }
        },
    )
    return require_audit_result(final_state)
```

### 5.2 当前工作单元

```python
def current_work_item(state):
    bid_file = state.bid_files[state.current_bid_index]

    # 文件适用性尚未判断时，先执行项目匹配判断。
    if bid_file.file_id not in state.project_matches:
        return ApplicabilityWorkItem(file=bid_file)

    # 不适用文件不会进入规则循环。
    if state.project_matches[bid_file.file_id].status == "not_matched":
        return FileAlreadyHandled()

    rules = applicable_rules_for_file(state.rule_set, bid_file)
    if not rules:
        return NoApplicableRulesWorkItem(file=bid_file)
    rule = rules[state.current_rule_index]
    return work_item_for_rule(rule)
```

### 5.3 bid_agent 节点

```python
async def bid_agent_node(state, tools: list[BaseTool]):
    work_item = current_work_item(state)

    if isinstance(work_item, DeterministicFindingWorkItem):
        # 冲突规则和人工规则直接形成受校验 Decision，不再请求模型判断。
        return decision_message(
            RecordFindingDecision(
                reason=work_item.reason,
                finding=work_item.finding,
            )
        )

    if isinstance(work_item, NoApplicableRulesWorkItem):
        return decision_message(
            build_no_applicable_rules_file_decision(work_item.file)
        )

    messages = build_bid_agent_messages(
        system_prompt=load_prompt("bid_agent"),
        task_identity=identity_from_state(state),
        work_item=work_item,
        prior_related_findings=relevant_findings_only(state, work_item),
        available_tools=tools,
        react_messages=state.messages,
    )

    model_with_tools = bid_chat_model.bind_tools(tools)
    response: AIMessage = await model_with_tools.ainvoke(messages)
    return {
        "messages": [response],
        "react_step_count": state.react_step_count + 1,
    }
```

`apply_bid_agent_output_node` 从最后一条无 tool call 的 AssistantMessage 解析 Decision。若达到步数上限仍没有合法 Decision，则普通代码按当前工作单元生成 `manual_review` Finding；适用性阶段则生成 `status=uncertain` 且继续审核的项目匹配结果。

`build_bid_agent_messages` 必须保留当前工作单元内完整的 AIMessage / ToolMessage 历史。Decision 写入 State 后，`apply_bid_agent_output_node` 清理本工作单元的 ReAct messages，避免下一条规则继承无关工具历史。

### 5.4 State 更新

```python
def apply_bid_agent_output(state, decision):
    # LLM 只能给出 Decision，游标和列表由普通代码维护。
    validated = BidAgentDecisionAdapter.validate_python(decision)

    if validated.action == "record_project_match":
        validate_project_match_for_current_file(state, validated.project_match)
        state.project_matches[current_file_id(state)] = validated.project_match

        if not validated.is_applicable:
            state.file_results.append(
                build_not_applicable_file_result(
                    current_file=state.bid_files[state.current_bid_index],
                    project_match=validated.project_match,
                    exclusion_reason=validated.exclusion_reason,
                )
            )
            state.current_bid_index += 1
            state.current_rule_index = 0

    elif validated.action == "record_file_result":
        validate_file_result_for_current_file(state, validated.file_result)
        append_unique_file_result(state, validated.file_result)
        state.current_bid_index += 1
        state.current_rule_index = 0

    elif validated.action == "record_finding":
        validate_finding_for_current_rule(state, validated.finding)
        append_unique_finding(state, validated.finding)
        append_unique_evidence_results(state, validated.evidence_results)
        state.current_rule_index += 1

        current_file = state.bid_files[state.current_bid_index]
        current_rules = applicable_rules_for_file(
            state.rule_set,
            current_file,
        )
        if state.current_rule_index >= len(current_rules):
            file_result = build_file_result_deterministically(
                current_file=current_file,
                project_match=state.project_matches[current_file.file_id],
                findings=findings_for_current_file(state),
                evidence=evidence_for_current_file(state),
            )
            state.file_results.append(file_result)
            state.current_bid_index += 1
            state.current_rule_index = 0

    state.react_step_count = 0
    return {
        **state_delta(state),
        **clear_current_react_messages(),
    }
```

### 5.5 规则冲突处理

```python
def work_item_for_rule(rule):
    if rule.status in {"rule_conflict", "manual_review"}:
        # 不做普通响应审核，直接根据规则来源形成需人工复核 Finding。
        return deterministic_manual_review_finding(rule)
    return rule
```

### 5.6 完成条件

```python
def check_completion(state) -> bool:
    if state.current_bid_index != len(state.bid_files):
        return False
    if len(state.file_results) != len(state.bid_files):
        return False

    for file_result in state.file_results:
        if not file_result.is_applicable:
            continue

        expected_rules = applicable_rules_for_file(
            state.rule_set,
            file_result.file,
        )
        if not every_rule_has_finding(expected_rules, file_result.findings):
            return False
        if not every_finding_has_rule_source(file_result.findings):
            return False
        if not every_response_is_traced_or_not_found(file_result.findings):
            return False
        if not evidence_requirements_satisfied(
            expected_rules,
            file_result.findings,
            file_result.evidence_results,
        ):
            return False

    return state.audit_result is not None or can_build_audit_result(state)
```

### 5.7 汇总结果

```python
def build_audit_result(state):
    ordered_results = order_by_input_files(
        state.bid_files,
        state.file_results,
    )
    return AuditResult(
        schema_version="1.0",
        task_id=state.task_id,
        project_id=state.project_id,
        bidder_id=state.bidder_id,
        file_results=ordered_results,
    )
```

## 6. 适用性判断

主 Agent 固定 Prompt 保存在 `app/prompts/templates/bid_agent.md`。当前文件、当前规则、历史 Finding 和工具定义由 `app/agents/bid_agent.py` 动态组装，不写进静态 Prompt 文件。

主 Agent 首先比较：

- 项目名称。
- 招标人。
- 物资类别。
- 包号。
- 其他可识别项目字段。

机械加工设备文件应先形成 `record_project_match`，由普通状态更新函数确定性生成 `is_applicable=false`、`overall_conclusion=not_applicable` 的 FileAuditResult，并且不得调用 `request_evidence_review`。

## 7. 异常与限额

- 主 Agent 最大 ReAct 步数。
- 单次模型超时。
- 工具有限重试。
- 单条规则失败尽量降级为 `manual_review` 后继续。
- State 损坏、身份不一致、最终 Schema 无法生成时任务失败。

## 8. 测试设计

- 两份相关投标文件进入完整规则循环。
- 异类文件提前结束且不调用子 Agent。
- 适用性判断可通过 `record_project_match` 推进，不会在首个文件上循环。
- `thread_id` 使用 task_id，同一任务的 checkpointer 状态不会串线。
- 未找到投标响应仍产生 Finding。
- evidence_required 且找到响应时能够产生 Claim 并调用子 Agent。
- LLM 不能跳过游标或重复写入 Finding。
- 规则全部处理后由普通函数汇总 FileAuditResult。
- 当前文件没有任何适用规则时形成可追踪的人工复核 FileAuditResult，不发生数组越界。
- `non_compliant > risk > manual_review > compliant` 的总体结论优先级正确。
- 步数超限只影响当前规则时生成人工复核并继续。

## 9. 验收标准

- 图内只有主 ReAct 和 Tool 内部子 ReAct 两个智能体。
- State 更新和任务完成判断由确定性函数执行。
- 每个输入文件恰好有一个 FileAuditResult。
- 每条适用规则都有 Finding。
- 最终 AuditResult 通过任务 02 的严格 Schema。
