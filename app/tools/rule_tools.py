"""规则专属检索和提交工具；会话范围由代码注入，不暴露业务 State。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

from langchain_core.tools import InjectedToolCallId, StructuredTool
from pydantic import Field

from app.models.common import StrictModel
from app.models.documents import ParsedDocument
from app.models.rules import NonBlank, RuleCommitRecord, RuleSet, SubmitRuleDeltaInput
from app.prompts.loader import load_tool_description
from app.retrieval.models import IndexFilter
from app.retrieval.service import RetrievalService
from app.rules.context import build_rule_view
from app.rules.merger import apply_rule_delta_operations


class RuleSearchGroup(StrictModel):
    queries: list[NonBlank] = Field(
        min_length=1,
        description="本组一个或多个明确问题，可用互补表达，避免重复同义查询。",
    )
    file_ids: list[NonBlank] = Field(
        min_length=1,
        description="本组所有查询的共同文件范围，仅可选已处理历史文件 ID，不能填当前或未处理文件。",
    )


class SearchRuleSourceInput(StrictModel):
    querylist: list[RuleSearchGroup] = Field(
        min_length=1,
        description="非空查询组列表；不同文件范围分别成组，每组必须明确 queries 和 file_ids。",
    )


class SubmitRuleToolInput(SubmitRuleDeltaInput):
    tool_call_id: Annotated[str, InjectedToolCallId]


@dataclass
class RuleSession:
    """单文件会话的权威规则状态；工具成功落盘后才发布新值。"""

    rule_set: RuleSet
    current: ParsedDocument
    history: dict[str, ParsedDocument]
    persist: Callable[[RuleSet, list[RuleCommitRecord]], Awaitable[None]]
    retrieval: RetrievalService
    commits: list[RuleCommitRecord] = field(default_factory=list)
    model_calls: int = 0
    unresolved_write_error: str | None = None
    cached_submissions: dict[str, tuple[dict, str]] = field(default_factory=dict)
    accessed_history: set[str] = field(default_factory=set)

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

    async def search_rule_source(querylist: list[RuleSearchGroup]) -> str:
        groups = []
        for number, group in enumerate(querylist, 1):
            lines = [
                f"## 查询组 {number}",
                "查询：" + "；".join(group.queries),
                "文件范围：" + "、".join(group.file_ids),
            ]
            try:
                if not set(group.file_ids) <= session.history.keys():
                    raise ValueError(
                        "只能回查已完成处理的历史文件，不能查询当前或未处理文件"
                    )
                parsed = [session.history[key] for key in group.file_ids]
                if not any(
                    block.content.strip()
                    or any(image.transcription.strip() for image in block.images)
                    for item in parsed
                    for section in item.sections
                    for block in section.blocks
                ):
                    raise ValueError("当前范围无可检索文字，请使用已提供的图片路径查看")
                hits = {}
                for query in dict.fromkeys(group.queries):
                    results = await session.retrieval.search(
                        query,
                        IndexFilter(
                            scope_id=f"rules-{session.rule_set.project_id}",
                            project_id=session.rule_set.project_id,
                            data_role="rule_source",
                            file_ids=group.file_ids,
                        ),
                    )
                    for result in results:
                        if (
                            result.source.file_id not in group.file_ids
                            or result.project_id != session.rule_set.project_id
                        ):
                            raise ValueError("检索返回越界文件")
                        expected = session.history[result.source.file_id]
                        if (
                            not expected.markdown_path
                            or result.parse_version != expected.markdown_path
                        ):
                            raise ValueError("索引解析版本与会话不一致，需要重建索引")
                        entry = hits.setdefault(result.chunk_id, (result, []))
                        entry[1].append(query)
                lines.append(
                    "状态：检索完成"
                    if hits
                    else "状态：检索完成，本次未召回；不表示原文没有要求"
                )
                used = 0
                budget = session.retrieval.settings.retrieval.max_source_chars
                for number_hit, (result, queries) in enumerate(hits.values(), 1):
                    if used + len(result.content) > budget and used:
                        lines.append("返回达到本组长度预算；其余命中未展示。")
                        break
                    if len(result.content) > budget:
                        lines.append("完整结构超过本组预算，不截断表格。")
                        content = (
                            "原文结构过长，未在此展示；请查看关联原图。"
                            if result.image_paths
                            else "原文结构过长，未在此展示；该结构没有可用原图，本次未能提供完整内容。"
                        )
                    else:
                        content = result.content
                    lines.extend(
                        [
                            f"### 命中 {number_hit}",
                            "命中查询：" + "；".join(queries),
                            f"文件：{result.source.file_id}（{result.source.file_name}）",
                            f"位置：{result.source.section or '章节未知'}；页码：{'、'.join(map(str, result.page_numbers)) or '未知'}",
                            f"原文：{content}",
                        ]
                    )
                    if result.image_paths:
                        registered = {
                            image.path
                            for section in session.history[
                                result.source.file_id
                            ].sections
                            for block in section.blocks
                            for image in block.images
                        }
                        if not set(result.image_paths) <= registered:
                            raise ValueError("索引图片未登记在固定解析版本，需要重建")
                        lines.append("相关图片：" + "、".join(result.image_paths))
                    if result.truncated:
                        lines.append("读取限制：此次返回为文本节选，不能代表完整条款。")
                    if content == result.content:
                        session.accessed_history.add(result.source.file_id)
                    used += len(content)
            except Exception as exc:
                lines.append(
                    f"状态：本组未执行或未完整完成。原因：{type(exc).__name__}: {exc}"
                )
            groups.append("\n\n".join(lines))
        return "\n\n".join(groups)

    return [
        StructuredTool.from_function(
            coroutine=search_rule_source,
            name="search_rule_source",
            args_schema=SearchRuleSourceInput,
            description=load_tool_description("search_rule_source"),
        ),
        StructuredTool.from_function(
            coroutine=submit_rule_delta,
            name="submit_rule_delta",
            args_schema=SubmitRuleToolInput,
            description=load_tool_description("submit_rule_delta"),
        ),
    ]
