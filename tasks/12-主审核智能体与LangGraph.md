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
app/prompts/bid_agent.py
```

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

    graph.add_node("bid_agent", bid_agent_node)
    graph.add_node("tools", ToolNode([
        search_bid_response,
        retrieve_rule_source,
        request_evidence_review,
    ]))
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
    return graph.compile(checkpointer=task_checkpointer)
```

### 5.2 当前工作单元

```python
def current_work_item(state):
    bid_file = state.bid_files[state.current_bid_index]

    # 文件适用性尚未判断时，先执行项目匹配判断。
    if not has_applicability_result(state, bid_file.file_id):
        return ApplicabilityWorkItem(file=bid_file)

    # 不适用文件不会进入规则循环。
    if is_file_not_applicable(state, bid_file.file_id):
        return FileAlreadyHandled()

    rule = state.rule_set.rules[state.current_rule_index]
    return RuleAuditWorkItem(file=bid_file, rule=rule)
```

### 5.3 bid_agent 节点

```python
async def bid_agent_node(state):
    work_item = current_work_item(state)
    messages = build_bid_agent_messages(
        task_identity=identity_from_state(state),
        work_item=work_item,
        prior_related_findings=relevant_findings_only(state, work_item),
        available_tools=BID_AGENT_TOOLS,
    )

    response = await chat_model.complete_with_tools(
        messages=messages,
        tools=BID_AGENT_TOOLS,
        final_output_model=BidAgentDecision,
    )
    return {
        "messages": [response.message],
        "react_step_count": state.react_step_count + 1,
    }
```

### 5.4 State 更新

```python
def apply_bid_agent_output(state, decision):
    # LLM 只能给出 Decision，游标和列表由普通代码维护。
    validated = BidAgentDecisionAdapter.validate_python(decision)

    if validated.action == "record_file_result":
        validate_file_result_for_current_file(state, validated.file_result)
        append_unique_file_result(state, validated.file_result)
        state.current_bid_index += 1
        state.current_rule_index = 0

    elif validated.action == "record_finding":
        validate_finding_for_current_rule(state, validated.finding)
        append_unique_finding(state, validated.finding)
        append_unique_evidence_results(state, validated.evidence_results)
        state.current_rule_index += 1

        if state.current_rule_index >= len(active_or_review_rules(state.rule_set)):
            file_result = build_file_result_deterministically(
                current_file=state.bid_files[state.current_bid_index],
                findings=findings_for_current_file(state),
                evidence=evidence_for_current_file(state),
            )
            state.file_results.append(file_result)
            state.current_bid_index += 1
            state.current_rule_index = 0

    state.react_step_count = 0
    return state_delta(state)
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

主 Agent 首先比较：

- 项目名称。
- 招标人。
- 物资类别。
- 包号。
- 其他可识别项目字段。

机械加工设备文件应形成 `record_file_result`，`is_applicable=false`、`overall_conclusion=not_applicable`，并且不得调用 `request_evidence_review`。

## 7. 异常与限额

- 主 Agent 最大 ReAct 步数。
- 单次模型超时。
- 工具有限重试。
- 单条规则失败尽量降级为 `manual_review` 后继续。
- State 损坏、身份不一致、最终 Schema 无法生成时任务失败。

## 8. 测试设计

- 两份相关投标文件进入完整规则循环。
- 异类文件提前结束且不调用子 Agent。
- 未找到投标响应仍产生 Finding。
- evidence_required 且找到响应时能够产生 Claim 并调用子 Agent。
- LLM 不能跳过游标或重复写入 Finding。
- 规则全部处理后由普通函数汇总 FileAuditResult。
- `non_compliant > risk > manual_review > compliant` 的总体结论优先级正确。
- 步数超限只影响当前规则时生成人工复核并继续。

## 9. 验收标准

- 图内只有主 ReAct 和 Tool 内部子 ReAct 两个智能体。
- State 更新和任务完成判断由确定性函数执行。
- 每个输入文件恰好有一个 FileAuditResult。
- 每条适用规则都有 Finding。
- 最终 AuditResult 通过任务 02 的严格 Schema。
