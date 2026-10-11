"""各Agent共用的检索、看图与轻量沙箱，以及文本编辑内部实现。"""

import ast
import asyncio
import json
import math
import os
import signal
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
from string import Template
from typing import Annotated, Callable

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, StructuredTool
from pydantic import Field, StringConstraints, model_validator

from app.core.config import AppSettings, PythonToolSettings
from app.models.common import StrictModel
from app.models.documents import ParsedDocument, validate_asset_path
from app.parsers.assets import AssetStore, file_asset_root
from app.parsers.image_messages import build_view_image_result
from app.prompts.loader import load_tool_description
from app.retrieval.models import IndexFilter, SearchResult

QueryText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def apply_text_edits(text, edits):
    """仅在内存副本按顺序精确替换；调用方全部成功后自行原子保存。"""
    candidate = text
    for index, edit in enumerate(edits):
        matches = candidate.count(edit.old_text)
        if matches != 1:
            reason = "未找到完全一致的原文。" if matches == 0 else "不能唯一定位。"
            guidance = (
                "请核对当前原文的引号、空白、换行及前项替换影响；"
                "需要时 read 获取当前稿后重建本批 edits。"
                if matches == 0
                else "请加入相邻标题或上下文，使该项在按顺序替换后的"
                "候选稿中仅命中一次。"
            )
            raise ValueError(
                f"edits[{index}].old_text（第{index + 1}项，共{len(edits)}项）："
                f"匹配{matches}处，{reason}\n"
                "匹配基于本批前面各项替换后的内存候选稿；"
                "整批未保存，磁盘仍为调用前版本。\n"
                + guidance
            )
        candidate = candidate.replace(edit.old_text, edit.new_text, 1)
    return candidate


class DocumentSearchGroup(StrictModel):
    queries: list[QueryText] = Field(
        min_length=1,
        max_length=12,
        description="本组检索问题列表，每项为非空问题文本；所有查询共用本组 file_ids，结果标明命中的查询。",
    )
    file_ids: list[QueryText] | None = Field(
        min_length=1,
        max_length=30,
        description="本组共同文件范围，填授权 ID 列表；显式 null 使用工具说明中的默认范围，绝非全库。",
    )


class DocumentChunkGroup(StrictModel):
    chunk_id: QueryText = Field(description="当前文件冻结版本中真实的中心片段ID，原样传入。")
    before: int = Field(ge=0, strict=True, description="中心前读取的片段数量；非负整数，0表示不向前扩展。")
    after: int = Field(ge=0, strict=True, description="中心后读取的片段数量；中心始终返回。")
    file_id: QueryText = Field(description="该中心片段所属的单个授权文件ID，不是file_ids列表。")


class SearchDocumentsInput(StrictModel):
    querylist: list[DocumentSearchGroup] | None = Field(
        default=None,
        min_length=1,
        max_length=12,
        description="非空查询组列表，每组有 queries 和 file_ids；不同范围分组。",
    )
    chunklist: list[DocumentChunkGroup] | None = Field(
        default=None, min_length=1,
        description="邻接读取组列表，每组为chunk_id、before、after、file_id；与querylist二选一，未用项省略或null。",
    )

    @model_validator(mode="after")
    def one_mode(self):
        if (self.querylist is None) == (self.chunklist is None):
            raise ValueError("querylist与chunklist必须恰好提供一个非空列表")
        return self


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

        def register(result):
            if self.on_source_read:
                self.on_source_read(result.source.file_id)
            if self.on_chunk_read:
                self.on_chunk_read(result)

        def read_neighbors(chunklist):
            files, errors = {}, []
            for number, group in enumerate(chunklist, 1):
                try:
                    if group.file_id not in self.parsed_documents:
                        raise ValueError("文件不在当前会话授权范围")
                    parsed = self.parsed_documents[group.file_id]
                    if group.file_id not in files:
                        scope = self.file_filters.get(group.file_id, self.query_filter)
                        chunks = self.retrieval.file_chunks(
                            scope, file_id=group.file_id, parse_version=parsed.markdown_path,
                        )
                        files[group.file_id] = (chunks, [])
                    chunks, windows = files[group.file_id]
                    center = next((i for i, c in enumerate(chunks) if c.chunk_id == group.chunk_id), None)
                    if center is None:
                        raise ValueError("片段ID不存在于指定文件的当前冻结索引；请核对file_id及真实chunk_id")
                    start, end = max(0, center - group.before), min(len(chunks), center + group.after + 1)
                    registered = {image.path for section in parsed.sections for block in section.blocks for image in block.images}
                    for chunk in chunks[start:end]:
                        if chunk.file_id != group.file_id or chunk.parse_version != parsed.markdown_path:
                            raise ValueError("索引片段与授权文件或解析版本不匹配")
                        if any(getattr(chunk, key) != getattr(parsed.document, key) for key in ("data_role", "project_id", "bidder_id", "task_id")):
                            raise ValueError("索引片段角色或归属与授权文件不符")
                        if not set(chunk.image_paths) <= registered:
                            raise ValueError("索引图片未登记在固定解析版本，需要重建索引")
                    windows.append((start, end))
                except Exception as exc:
                    errors.append(f"请求组{number}失败：{type(exc).__name__}: {exc}")
            sections, delivered = [], []
            for chunks, windows in files.values():
                merged = []
                for start, end in sorted(windows):
                    if merged and start < merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                    else:
                        merged.append((start, end))
                for start, end in merged:
                    parts = []
                    for index in range(start, end):
                        chunk = chunks[index]
                        lines = [f"### 片段 {chunk.chunk_id}",
                                 f"文件：{chunk.file_id}（{chunk.file_name}）",
                                 f"位置：{chunk.section or '章节未知'}；页码：{'、'.join(map(str, chunk.page_numbers)) or '未知'}"]
                        boundary = []
                        if index == 0:
                            boundary.append("当前索引开头")
                        if index == len(chunks) - 1:
                            boundary.append("当前索引末尾")
                        if boundary:
                            lines.append("边界：" + "、".join(boundary))
                        lines.append("原文：\n" + chunk.content)
                        if chunk.image_paths:
                            lines.append("相关图片：" + "、".join(chunk.image_paths))
                        parts.append("\n\n".join(lines))
                        delivered.append(SearchResult(
                            chunk_id=chunk.chunk_id, source=chunk.source_ref(len(chunk.content)),
                            content=chunk.content, parse_version=chunk.parse_version,
                            page_numbers=chunk.page_numbers, image_paths=chunk.image_paths,
                            project_id=chunk.project_id, bidder_id=chunk.bidder_id, task_id=chunk.task_id,
                            evidence_type=chunk.evidence_type,
                        ))
                    sections.append("\n\n".join(parts))
            if errors:
                sections.append(("状态：部分完成。" if delivered else "状态：未完成。") + "\n\n" + "\n".join(errors))
            # 完成范围检查及投影后才登记，未交付组不能成为已读来源。
            for result in delivered:
                register(result)
            return "\n\n---\n\n".join(sections)

        async def search_documents(querylist=None, chunklist=None) -> str:
            if chunklist is not None:
                return read_neighbors(chunklist)
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
                    # Task32 D1：暂时停用组正文预算，保留Top-K/Rerank及组内去重。
                    # used = 0
                    # budget = self.settings.retrieval.max_source_chars
                    for result, queries in hits.values():
                        # 旧裁剪仅留作恢复参考，不把别组命中冒充本组结果：
                        # if used and used + len(result.content) > budget:
                        #     lines.append("返回达到本组长度预算；其余命中未展示。")
                        #     break
                        # displayed = len(result.content) <= budget
                        # content = result.content if displayed else "完整结构超过本组预算，原文未展示，不截断表格。"
                        content = result.content
                        lines.append(f"### 片段 {result.chunk_id}")
                        lines.extend(
                            [
                                "命中查询：" + "；".join(queries),
                                f"文件：{result.source.file_id}（{result.source.file_name}）",
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
                        register(result)
                        # used += len(content)
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
            description=Template(load_tool_description("search_documents")).substitute(
                candidate_top_k=self.settings.retrieval.candidate_top_k,
                final_top_k=self.settings.retrieval.final_top_k,
            )
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
        result.append(build_python_tool(self.settings.python_tool))
        return result


# 能力而非任意模块透传，避免借标准库的公共导入/文件函数绕过预检。
EXPORTS = {
    "math": tuple(name for name in dir(math) if not name.startswith("_")),
    "decimal": (
        "Decimal", "Context", "getcontext", "localcontext", "setcontext",
        "ROUND_CEILING", "ROUND_DOWN", "ROUND_FLOOR", "ROUND_HALF_DOWN",
        "ROUND_HALF_EVEN", "ROUND_HALF_UP", "ROUND_UP", "ROUND_05UP",
    ),
    "datetime": ("date", "datetime", "time", "timedelta", "timezone", "MINYEAR", "MAXYEAR", "UTC"),
    "statistics": (
        "mean", "fmean", "geometric_mean", "harmonic_mean", "median", "median_low",
        "median_high", "median_grouped", "mode", "multimode", "quantiles", "pstdev",
        "pvariance", "stdev", "variance", "correlation", "covariance", "linear_regression",
    ),
    "collections": ("Counter", "defaultdict", "deque", "OrderedDict", "ChainMap"),
    "itertools": (
        "accumulate", "chain", "combinations", "combinations_with_replacement", "compress",
        "count", "cycle", "dropwhile", "filterfalse", "groupby", "islice", "pairwise",
        "permutations", "product", "repeat", "starmap", "takewhile", "tee", "zip_longest",
    ),
    "json": ("loads", "dumps"),
    "re": (
        "compile", "escape", "findall", "finditer", "fullmatch", "match", "search", "split",
        "sub", "subn", "ASCII", "IGNORECASE", "MULTILINE", "DOTALL", "VERBOSE", "NOFLAG",
    ),
    "string": (
        "ascii_letters", "ascii_lowercase", "ascii_uppercase", "digits", "hexdigits",
        "octdigits", "punctuation", "printable", "whitespace", "capwords",
    ),
}
BUILTINS = (
    "abs", "all", "any", "ascii", "bin", "bool", "chr", "dict", "divmod", "enumerate",
    "filter", "float", "format", "frozenset", "hex", "int", "isinstance", "len", "list",
    "map", "max", "min", "next", "oct", "ord", "pow", "print", "range", "repr", "reversed",
    "round", "set", "slice", "sorted", "str", "sum", "tuple", "zip",
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError", "ZeroDivisionError",
)
DENIED_NAMES = {
    "open", "input", "eval", "exec", "compile", "globals", "locals", "vars", "dir",
    "getattr", "setattr", "delattr", "hasattr", "type", "object", "super", "help",
    "breakpoint", "exit", "quit", "memoryview",
}
DENIED_ATTRIBUTES = {
    "format", "format_map", "gi_frame", "gi_code", "gi_yieldfrom",
    "cr_frame", "cr_code", "cr_await", "ag_frame", "ag_code", "ag_await",
    "tb_frame", "tb_next", "f_back", "f_globals", "f_locals", "f_builtins", "f_code",
}


def validate_code(code: str, settings: PythonToolSettings) -> None:
    """全量预检通过才启动进程；这里不是任意Python的安全证明。"""
    if not code.strip():
        raise ValueError("代码不能为空")
    if len(code.encode("utf-8")) > settings.max_code_bytes:
        raise ValueError(f"代码超过 {settings.max_code_bytes} 字节上限")
    tree = ast.parse(code, filename="<sandbox>")
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 1)
        reason = None
        if isinstance(node, ast.Import):
            denied = next((alias.name for alias in node.names if alias.name not in EXPORTS), None)
            if denied:
                reason = f"禁止导入模块 {denied!r}；仅支持白名单中开放的成员"
            private = next(((a.asname or a.name) for a in node.names if (a.asname or a.name).startswith("_")), None)
            if private:
                reason = f"导入名称或别名 {private!r} 触发当前下划线命名限制；请使用普通名称，导入仍须在白名单内"
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in EXPORTS:
                reason = f"禁止导入模块 {('.' * node.level + (node.module or ''))!r}；仅支持白名单内的绝对导入"
            else:
                denied = next((a.name for a in node.names if a.name not in EXPORTS[node.module]), None)
                if denied:
                    reason = f"模块 {node.module!r} 未开放成员 {denied!r}"
                private = next(((a.asname or a.name) for a in node.names if (a.asname or a.name).startswith("_")), None)
                if private:
                    reason = f"导入名称或别名 {private!r} 触发当前下划线命名限制；请使用普通名称，成员仍须在白名单内"
        elif isinstance(node, ast.Name) and (node.id.startswith("_") or node.id in DENIED_NAMES):
            reason = (
                f"标识符 {node.id!r} 触发当前下划线命名限制；可改为 item 等普通变量名"
                if node.id.startswith("_")
                else f"禁止使用标识符 {node.id!r}；仅支持已提供数据的内存处理，不开放外部访问或动态执行等受限能力"
            )
        elif isinstance(node, ast.Attribute) and (
            node.attr.startswith("_") or node.attr in DENIED_ATTRIBUTES
            or isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            if node.attr.startswith("_") or node.attr in DENIED_ATTRIBUTES:
                reason = f"禁止访问私有或反射属性/方法 {node.attr!r}"
            else:
                reason = f"不支持属性赋值或删除：{node.attr!r}"
        elif isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef, ast.Await, ast.With, ast.AsyncWith, ast.Global, ast.Nonlocal)):
            reason = f"当前不支持语法 {type(node).__name__}"
        elif isinstance(node, (ast.FunctionDef, ast.arg)):
            name = node.name if isinstance(node, ast.FunctionDef) else node.arg
            if name.startswith("_") or name in DENIED_NAMES:
                reason = (
                    f"函数或形参 {name!r} 触发当前下划线命名限制；请使用普通名称"
                    if name.startswith("_")
                    else f"函数或形参不能使用受限标识符 {name!r}"
                )
        if reason:
            raise PermissionError(f"第{line}行：{reason}。本次代码未执行。")


# 固定启动器仅在独立子进程执行，不在业务进程exec；不导入项目或第三方包。
RUNNER = r'''
import builtins, importlib, os, resource, signal, sys, traceback, types
resource.setrlimit(resource.RLIMIT_CPU, (int(sys.argv[1]), int(sys.argv[1])))
if sys.platform == "linux":
    resource.setrlimit(resource.RLIMIT_AS, (int(sys.argv[2]), int(sys.argv[2])))
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
signal.alarm(int(sys.argv[1]))
os.environ.clear()
exports = EXPORTS_VALUE
modules = {}
for name, members in exports.items():
    module = importlib.import_module(name)
    modules[name] = types.SimpleNamespace(**{key: getattr(module, key) for key in members})
# datetime的C实现内部需要这两个模块；模型代码的直接导入仍被AST拒绝。
internals = {name: importlib.import_module(name) for name in ("time", "_strptime")}
def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if not level and name in internals:
        return internals[name]
    if level or name not in modules:
        raise PermissionError("禁止导入：" + name)
    return modules[name]
safe = {name: getattr(builtins, name) for name in BUILTINS_VALUE}
safe["__import__"] = safe_import
try:
    code = sys.stdin.buffer.read().decode("utf-8")
    exec(compile(code, "<sandbox>", "exec"), {"__builtins__": safe, "__name__": "sandbox"})
except BaseException as error:
    frames = [frame for frame in traceback.extract_tb(error.__traceback__) if frame.filename == "<sandbox>"]
    line = frames[-1].lineno if frames else getattr(error, "lineno", None)
    location = ("第" + str(line) + "行 ") if line else ""
    print(type(error).__name__ + ": " + location + str(error), file=sys.stderr)
    sys.exit(1)
'''.replace("EXPORTS_VALUE", repr(EXPORTS)).replace("BUILTINS_VALUE", repr(BUILTINS))


class OutputLimitError(RuntimeError):
    pass


class SandboxUnavailable(RuntimeError):
    pass


_slots_lock = threading.Lock()
_active = 0


@asynccontextmanager
async def execution_slot(limit: int):
    """四类工厂共享进程级名额，不绑定特定事件循环或模型请求信号量。"""
    global _active
    while True:
        with _slots_lock:
            if _active < limit:
                _active += 1
                break
        await asyncio.sleep(0.02)
    try:
        yield
    finally:
        with _slots_lock:
            _active -= 1


async def _execute_process(code: str, settings: PythonToolSettings, cwd: str):
    """固定解释器、空工作目录、最小环境，有界读取并在任何出口回收进程组。"""
    limit = settings.max_output_bytes
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-S", "-B", "-u", "-c", RUNNER,
        str(math.ceil(settings.timeout_seconds)), str(settings.memory_mb * 1024 * 1024),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=cwd,
        env={"LANG": "C.UTF-8"}, start_new_session=True,
    )
    used = 0

    async def read(stream):
        nonlocal used
        parts = []
        while chunk := await stream.read(4096):
            used += len(chunk)
            if used > limit:
                raise OutputLimitError(f"输出超过 {limit} 字节上限")
            parts.append(chunk)
        return b"".join(parts).decode("utf-8", errors="replace")

    async def write():
        try:
            process.stdin.write(code.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    jobs = [asyncio.create_task(fn) for fn in (read(process.stdout), read(process.stderr), write(), process.wait())]
    try:
        out, err, _, rc = await asyncio.wait_for(asyncio.gather(*jobs), settings.timeout_seconds)
        return rc, out, err
    finally:
        for job in jobs:
            if not job.done():
                job.cancel()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.gather(*jobs, return_exceptions=True)
        await process.wait()


class PythonSandbox:
    def __init__(self, settings: PythonToolSettings):
        self.settings = settings

    async def execute(self, code: str):
        s = self.settings
        try:
            validate_code(code, s)
            if not s.enabled:
                raise SandboxUnavailable("沙箱工具已禁用")
            if sys.platform not in {"darwin", "linux"}:
                raise SandboxUnavailable("当前轻量执行环境只支持macOS/Linux的进程资源限制")
            async with execution_slot(s.max_concurrency):
                with tempfile.TemporaryDirectory(prefix="dw-python-") as cwd:
                    rc, out, err = await _execute_process(code, s, cwd)
                    if rc:
                        if rc in {-signal.SIGALRM, -signal.SIGXCPU}:
                            raise TimeoutError(f"超过 {s.timeout_seconds:g} 秒执行上限")
                        if rc < 0:
                            return {"status": "error", "result": f"ProcessError: 执行进程被信号 {-rc} 终止"}
                        return {"status": "error", "result": err.strip() or f"RuntimeError: 沙箱退出码 {rc}"}
                    return {"status": "success", "result": out}
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return {"status": "error", "result": f"TimeoutError: 超过 {s.timeout_seconds:g} 秒执行上限"}
        except Exception as error:
            return {"status": "error", "result": f"{type(error).__name__}: {error}"}


class PythonInput(StrictModel):
    code: str = Field(description="待执行的Python代码，不含Markdown围栏；数据直接写入代码，用print输出结果。")
    tool_call_id: Annotated[str, InjectedToolCallId]


def build_python_tool(settings: PythonToolSettings) -> StructuredTool:
    sandbox = PythonSandbox(settings)

    async def run_python(code: str, tool_call_id: Annotated[str, InjectedToolCallId]):
        result = await sandbox.execute(code)
        return ToolMessage(
            name="run_python", tool_call_id=tool_call_id, status=result["status"],
            content=json.dumps(result, ensure_ascii=False),
        )

    return StructuredTool.from_function(
        coroutine=run_python, name="run_python", args_schema=PythonInput,
        description=load_tool_description("run_python"),
    )
