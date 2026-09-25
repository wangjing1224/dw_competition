"""各 Agent 共用的工具提供类；每个会话单独注入文件范围和解析快照。"""

from typing import Annotated, Callable

from langchain_core.tools import InjectedToolCallId, StructuredTool
from pydantic import Field, StringConstraints

from app.core.config import AppSettings
from app.models.common import StrictModel
from app.models.documents import ParsedDocument, validate_asset_path
from app.parsers.assets import AssetStore, file_asset_root
from app.parsers.image_messages import build_view_image_result
from app.prompts.loader import load_tool_description
from app.retrieval.models import IndexFilter

QueryText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DocumentSearchGroup(StrictModel):
    queries: list[QueryText] = Field(
        min_length=1,
        max_length=12,
        description="本组明确问题，可查询不同事项或提供互补表达；不要堆重复同义词。",
    )
    file_ids: list[QueryText] | None = Field(
        min_length=1,
        max_length=30,
        description="本组共同文件范围，填授权 ID 列表；显式 null 使用工具说明中的默认范围，绝非全库。",
    )


class SearchDocumentsInput(StrictModel):
    querylist: list[DocumentSearchGroup] = Field(
        min_length=1,
        max_length=12,
        description="非空查询组列表，每组有 queries 和 file_ids；不同范围分组。",
    )


class ViewImageInput(StrictModel):
    path: str = Field(
        description="输入或检索结果中的图片路径，如 file-2/images/1.png，原样传入，不填写磁盘绝对路径或 URL。"
    )
    tool_call_id: Annotated[str, InjectedToolCallId]


class CommonTool:
    def __init__(
        self,
        *,
        parsed_documents: dict[str, ParsedDocument],
        settings: AppSettings,
        retrieval=None,
        query_filter: IndexFilter | None = None,
        default_file_ids: list[str] | None = None,
        current_file_id: str | None = None,
        on_source_read: Callable[[str], None] | None = None,
        file_filters: dict[str, IndexFilter] | None = None,
        on_chunk_read: Callable | None = None,
        on_image_read: Callable[[str], None] | None = None,
        strict_errors: bool = False,
    ):
        # 仅接收当前业务授权范围，不在这里推测 project/bidder/task 归属。
        self.parsed_documents = dict(parsed_documents)
        self.settings = settings
        self.retrieval = retrieval
        self.query_filter = query_filter
        self.default_file_ids = list(default_file_ids or [])
        self.current_file_id = current_file_id
        self.on_source_read = on_source_read
        self.file_filters = dict(file_filters or {})
        self.on_chunk_read = on_chunk_read
        self.on_image_read = on_image_read
        self.strict_errors = strict_errors
        if not set(self.default_file_ids) <= self.parsed_documents.keys():
            raise ValueError("默认检索范围必须是授权文件的子集")

    def search_tool(self) -> StructuredTool:
        if self.retrieval is None or (
            self.query_filter is None and not self.file_filters
        ):
            raise ValueError("公共检索需要业务注入检索服务和范围")
        for file_id, parsed in self.parsed_documents.items():
            scope = self.file_filters.get(file_id, self.query_filter)
            if scope is None:
                raise ValueError("授权文件缺少检索范围")
            if any(
                getattr(parsed.document, key) != getattr(scope, key)
                for key in ("data_role", "project_id", "bidder_id", "task_id")
            ):
                raise ValueError("授权文件角色或归属与检索范围不符")

        async def search_scopes(query, ids):
            # 多角色查询先按代码绑定的索引分组，不接受模型传 scope/project/bidder。
            scopes = {}
            for file_id in ids:
                scope = self.file_filters.get(file_id, self.query_filter)
                scopes.setdefault(scope.scope_id, (scope, []))[1].append(file_id)
            found = []
            for scope, selected in scopes.values():
                try:
                    results = await self.retrieval.search(
                        query, scope.model_copy(update={"file_ids": selected})
                    )
                except Exception as exc:
                    if self.strict_errors:
                        raise RuntimeError(f"原文检索执行失败：{exc}") from exc
                    raise
                for result in results:
                    if result.source.file_id not in selected or any(
                        getattr(result, key) != getattr(scope, key)
                        for key in ("project_id", "bidder_id", "task_id")
                    ):
                        error = RuntimeError if self.strict_errors else ValueError
                        raise error("检索返回越界文件")
                    found.append(result)
            return sorted(found, key=lambda result: result.score, reverse=True)

        async def search_documents(querylist: list[DocumentSearchGroup]) -> str:
            groups = []
            for number, group in enumerate(querylist, 1):
                ids = list(
                    dict.fromkeys(
                        self.default_file_ids
                        if group.file_ids is None
                        else group.file_ids
                    )
                )
                lines = [
                    f"## 查询组 {number}",
                    "查询：" + "；".join(group.queries),
                    "文件范围：" + ("、".join(ids) or "空"),
                ]
                try:
                    if not set(ids) <= self.parsed_documents.keys():
                        raise ValueError(
                            "只能查询当前会话授权文件，不能查询未来或越界文件"
                        )
                    if not ids:
                        lines.append("状态：当前无可检索历史文件，不扩大范围。")
                        groups.append("\n\n".join(lines))
                        continue
                    if self.current_file_id in ids:
                        lines.append("当前文件补查可能涉及后续正文，不推进阅读进度。")
                    if not any(
                        block.content.strip()
                        or any(image.transcription.strip() for image in block.images)
                        for key in ids
                        for section in self.parsed_documents[key].sections
                        for block in section.blocks
                    ):
                        raise ValueError(
                            "当前范围无可检索文字，请使用解析内容或详细摘要中的图片路径查看"
                        )
                    hits = {}
                    for query in dict.fromkeys(group.queries):
                        results = await search_scopes(query, ids)
                        for result in results:
                            parsed = self.parsed_documents[result.source.file_id]
                            if (
                                not parsed.markdown_path
                                or result.parse_version != parsed.markdown_path
                            ):
                                error = (
                                    RuntimeError if self.strict_errors else ValueError
                                )
                                raise error("索引解析版本与会话不一致，需要重建索引")
                            registered = {
                                image.path
                                for section in parsed.sections
                                for block in section.blocks
                                for image in block.images
                            }
                            if not set(result.image_paths) <= registered:
                                error = (
                                    RuntimeError if self.strict_errors else ValueError
                                )
                                raise error(
                                    "索引图片未登记在固定解析版本，需要重建索引"
                                )
                            hits.setdefault(result.chunk_id, (result, []))[1].append(
                                query
                            )
                    lines.append(
                        "状态：检索完成"
                        if hits
                        else "状态：检索完成，本次未召回；不代表原文没有相关内容"
                    )
                    used = 0
                    budget = self.settings.retrieval.max_source_chars
                    # 每组去重、预算；不把其他查询组的命中冒充本组结果。
                    for hit_number, (result, queries) in enumerate(hits.values(), 1):
                        if used and used + len(result.content) > budget:
                            lines.append("返回达到本组长度预算；其余命中未展示。")
                            break
                        displayed = len(result.content) <= budget
                        content = (
                            result.content
                            if displayed
                            else "完整结构超过本组预算，原文未展示，不截断表格。"
                        )
                        lines.append(f"### 命中 {hit_number}")
                        if displayed:
                            # 直接展示索引中的真实 ID；未展示正文不能作为已读依据。
                            lines.append(f"片段：{result.chunk_id}")
                        lines.extend(
                            [
                                "命中查询：" + "；".join(queries),
                                f"文件：{result.source.file_id}（{result.source.file_name}）",
                                "角色："
                                + {
                                    "rule_source": "招标原文",
                                    "bid_document": "当前投标原文",
                                    "evidence_material": "投标方资料",
                                }[
                                    str(
                                        self.parsed_documents[
                                            result.source.file_id
                                        ].document.data_role
                                    )
                                ],
                                f"位置：{result.source.section or '章节未知'}；页码：{'、'.join(map(str, result.page_numbers)) or '未知'}",
                                "原文：\n" + content,
                            ]
                        )
                        if result.image_paths:
                            lines.append("相关图片：" + "、".join(result.image_paths))
                        if result.truncated:
                            lines.append(
                                "读取限制：此次返回为文本节选，不代表完整原文。"
                            )
                        if displayed and self.on_source_read:
                            self.on_source_read(result.source.file_id)
                        if displayed and self.on_chunk_read:
                            self.on_chunk_read(result)
                        used += len(content)
                except Exception as exc:
                    if self.strict_errors and not isinstance(exc, ValueError):
                        raise
                    lines.append(
                        f"状态：本组未执行或未完整完成。原因：{type(exc).__name__}: {exc}"
                    )
                groups.append("\n\n".join(lines))
            return "\n\n".join(groups)

        return StructuredTool.from_function(
            coroutine=search_documents,
            name="search_documents",
            args_schema=SearchDocumentsInput,
            description=load_tool_description("search_documents")
            + "\n授权文件："
            + "、".join(self.parsed_documents)
            + "\nfile_ids=null 默认范围："
            + ("、".join(self.default_file_ids) or "空；当前无历史文件")
            + "。显式列表只能包含以上授权 ID。",
        )

    def tools(self) -> list[StructuredTool]:
        async def view_image(
            path: str, tool_call_id: Annotated[str, InjectedToolCallId]
        ):
            validate_asset_path(path)
            file_id = path.split("/", 1)[0]
            if file_id not in self.parsed_documents:
                raise ValueError("图片文件不在当前任务授权范围")
            parsed = self.parsed_documents[file_id]
            settings = self.settings.parsing
            result = build_view_image_result(
                parsed=parsed,
                image_path=path,
                assets=AssetStore(
                    file_asset_root(self.settings.storage.data_root, file_id),
                    max_bytes=settings.max_image_bytes,
                    max_pixels=settings.max_image_pixels,
                ),
                allowed_file_ids=set(self.parsed_documents),
                tool_call_id=tool_call_id,
                max_side=settings.vision_image_max_side,
            )
            result.name = "view_image"
            if self.on_image_read:
                self.on_image_read(path)
            return result

        result = [
            StructuredTool.from_function(
                coroutine=view_image,
                name="view_image",
                args_schema=ViewImageInput,
                description=load_tool_description("view_image"),
            )
        ]
        if self.retrieval is not None:
            result.insert(0, self.search_tool())
        return result
