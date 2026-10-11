"""规则专属检索和提交工具；会话范围由代码注入，不暴露业务 State。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

from langchain_core.tools import InjectedToolCallId, StructuredTool

from app.models.documents import ParsedDocument
from app.models.rules import RuleCandidatesInput, RuleCommitRecord, RuleSet, SubmitRuleDeltaInput
from app.prompts.loader import load_tool_description
from app.retrieval.models import IndexFilter
from app.retrieval.service import RetrievalService
from app.rules.context import CANDIDATE_TEMPLATE, build_rule_view, candidate_status, rule_working_context, validate_candidates
from app.rules.merger import apply_rule_delta_operations
from app.rules.policies import handle_rule_limit_exceeded
from app.tools.common_tools import apply_text_edits


class SubmitRuleToolInput(SubmitRuleDeltaInput):
    tool_call_id: Annotated[str, InjectedToolCallId]


def active_rule_tools(tools, phase):
    hidden = {"submit_rule_delta", "search_documents"} if phase == "extract" else {"rule_candidates"}
    return [tool for tool in tools if tool.name not in hidden]


@dataclass
class RuleSession:
    """单文件会话的权威规则状态；工具成功落盘后才发布新值。"""

    rule_set: RuleSet
    current: ParsedDocument
    history: dict[str, ParsedDocument]
    persist: Callable[[RuleSet, list[RuleCommitRecord]], Awaitable[None]]
    retrieval: RetrievalService
    rule_limit: int
    commits: list[RuleCommitRecord] = field(default_factory=list)
    model_calls: int = 0
    unresolved_write_error: str | None = None
    cached_submissions: dict[str, tuple[dict, str]] = field(default_factory=dict)
    accessed_history: set[str] = field(default_factory=set)
    # 以下仅为代码侧运行进度，不进入规则库协议或工具参数。
    next_unit_index: int = 0
    batch_start: int = 0
    batch_end: int = 0
    batch_index: int = 0
    file_model_calls: int = 0
    compaction_calls: int = 0
    compaction_records: list[dict] = field(default_factory=list)
    last_budget: dict = field(default_factory=dict)
    candidate_draft: str = CANDIDATE_TEMPLATE
    phase: str = "extract"
    candidate_error: str | None = None
    merge_tool_round_seen: bool = False

    async def save_candidates(self, draft, phase):
        previous = self.candidate_draft, self.phase
        self.candidate_draft, self.phase = draft, phase
        try:
            await self.persist(self.rule_set, self.commits)
        except BaseException:
            self.candidate_draft, self.phase = previous
            raise

    async def candidates(self, action, edits=None):
        if self.phase != "extract":
            raise ValueError("候选工具仅在提取阶段可用")
        if action == "read":
            return candidate_status(self) + "\n\n" + self.candidate_draft
        if action == "finish":
            if self.candidate_error:
                raise ValueError("存在未解决候选编辑错误：" + self.candidate_error)
            validate_candidates(self.candidate_draft)
            await self.save_candidates(self.candidate_draft, "merge")
            return "提取已交接；下轮提供合并工具。\n" + rule_working_context(self)
        try:
            candidate = apply_text_edits(self.candidate_draft, edits or [])
            old_error = self.candidate_error
            self.candidate_error = None
            try:
                await self.save_candidates(candidate, "extract")
            except BaseException:
                self.candidate_error = old_error
                raise
        except Exception as exc:
            self.candidate_error = str(exc)
            raise
        # 暂停return_view及正文回显，保留原实现便于后续对照；保存和错误反馈不变。
        # view = candidate if return_view == "all" else "\n\n".join(edit.new_text or "（删除）" for edit in edits or [])
        # return "候选稿已保存，尚未交接。\n" + candidate_status(self) + "\n\n" + view
        return "候选稿已保存，尚未交接。\n" + candidate_status(self)

    async def submit(self, request: SubmitRuleDeltaInput, call_id: str) -> str:
        if request.return_view is None:
            return build_rule_view(self.rule_set)
        payload = request.model_dump(mode="json", exclude_unset=True)
        if call_id in self.cached_submissions:
            previous, view = self.cached_submissions[call_id]
            if previous != payload:
                raise ValueError("同一 tool_call_id 不能用于不同提交")
            return view
        current_id = self.current.document.file_id
        for op in request.operations or []:
            if op.action == "replace":
                old = next(
                    (
                        rule
                        for rule in self.rule_set.rules
                        if rule.rule_id == op.rule_id
                    ),
                    None,
                )
                existing = {source.file_id for source in old.sources} if old else set()
                added_history = set(op.source_file_ids) - existing - {current_id}
                if not added_history <= self.accessed_history:
                    raise ValueError("replace 新增的历史来源须先实际回查，不能猜测来源")
        applied = apply_rule_delta_operations(
            self.rule_set,
            request.operations or [],
            current_file_id=current_id,
            allowed_file_ids={current_id, *self.history},
            project_profile=request.project_profile,
        )
        # 检查整批执行后的数量，不在中间 add 时提前触发；后续 merge 可能减少条数。
        if len(applied.rule_set.rules) > self.rule_limit:
            handle_rule_limit_exceeded(applied.rule_set, limit=self.rule_limit)
        record = RuleCommitRecord(
            tool_call_id=call_id,
            before=[
                rule
                for rule in self.rule_set.rules
                if rule.rule_id in applied.changed_ids | applied.absorbed_ids.keys()
            ],
            after=[
                rule
                for rule in applied.rule_set.rules
                if rule.rule_id in applied.changed_ids
            ],
            absorbed_ids=applied.absorbed_ids,
        )
        commits = [*self.commits, record]
        view = build_rule_view(
            applied.rule_set,
            rule_ids=applied.changed_ids if request.return_view == "changed" else None,
            include_profile=request.return_view == "all"
            or request.project_profile is not None,
        )
        # 副本保存失败不能改内存、返回候选视图或留下半批变更。
        await self.persist(applied.rule_set, commits)
        self.rule_set, self.commits = applied.rule_set, commits
        self.unresolved_write_error = None
        self.cached_submissions[call_id] = (payload, view)
        return view


def build_rule_tools(session: RuleSession) -> list[StructuredTool]:
    async def rule_candidates(action, edits=None):
        return await session.candidates(action, edits)

    async def submit_rule_delta(
        operations,
        project_profile,
        return_view,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ):
        # 工具已完成 Pydantic 验证，不经 model_dump 再把未传字段变为 null。
        request = SubmitRuleDeltaInput(
            operations=operations,
            project_profile=project_profile,
            return_view=return_view,
        )
        return await session.submit(request, tool_call_id)

    from app.tools.common_tools import CommonTool

    # 检索实现公共化，规则侧仍保留实际已展示来源的业务记录。
    search = CommonTool(
        parsed_documents={
            **session.history,
            session.current.document.file_id: session.current,
        },
        settings=session.retrieval.settings,
        retrieval=session.retrieval,
        query_filter=IndexFilter(
            scope_id=f"rules-{session.rule_set.project_id}",
            project_id=session.rule_set.project_id,
            data_role="rule_source",
        ),
        default_file_ids=[*session.history, session.current.document.file_id],
        current_file_id=session.current.document.file_id,
        on_source_read=session.accessed_history.add,
    ).search_tool()
    return [
        search,
        StructuredTool.from_function(
            coroutine=rule_candidates,
            name="rule_candidates",
            args_schema=RuleCandidatesInput,
            description=load_tool_description("rule_candidates"),
        ),
        StructuredTool.from_function(
            coroutine=submit_rule_delta,
            name="submit_rule_delta",
            args_schema=SubmitRuleToolInput,
            description=load_tool_description("submit_rule_delta"),
        ),
    ]
