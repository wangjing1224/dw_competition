"""入库专属保存/查看工具；只写当前文件，不接受模型提供的写入文件 ID。"""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Annotated

from langchain_core.tools import InjectedToolCallId, StructuredTool

from app.core.config import AppSettings
from app.evidence.context import render_material
from app.evidence.storage import write_library
from app.models.documents import ParsedDocument
from app.models.evidence import (
    EvidenceLibrary,
    EvidenceMaterial,
    SaveEvidenceMaterialInput,
)
from app.prompts.loader import load_tool_description


class SaveMaterialToolInput(SaveEvidenceMaterialInput):
    tool_call_id: Annotated[str, InjectedToolCallId]


@dataclass
class EvidenceSession:
    library: EvidenceLibrary
    current: ParsedDocument
    history: dict[str, ParsedDocument]
    settings: AppSettings
    counter: object
    detail_budget_tokens: int
    model_calls: int = 0
    file_model_calls: int = 0
    unresolved_write_error: str | None = None
    cached_submissions: dict = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def file_id(self):
        return self.current.document.file_id

    async def apply(self, request: SaveEvidenceMaterialInput, call_id: str) -> str:
        if request.material is None:
            ids = (
                list(self.history)
                if request.file_ids == "all"
                else list(dict.fromkeys(request.file_ids))
            )
            if not set(ids) <= self.history.keys():
                raise ValueError(
                    "只能查看本批已完成历史文件，不包含当前、未来或其他投标方文件"
                )
            if not ids:
                return "当前无已完成历史资料。"
            saved = {item.file_id for item in self.library.materials}
            if not set(ids) <= saved:
                raise ValueError("历史文件缺少已保存摘要")
            parts, used = [], 0
            for position, file_id in enumerate(ids):
                view = render_material(self.library, file_id)
                size = self.counter.count_text(view)
                if used + size > self.detail_budget_tokens:
                    parts.append(
                        "读取预算不足，以下文件详情未展示："
                        + "、".join(ids[position:])
                        + "。请缩小查看范围；不表示这些文件没有资料。"
                    )
                    break
                parts.append(view)
                used += size
            return "\n\n".join(parts)

        async with self.lock:
            payload = request.model_dump(mode="json")
            if call_id in self.cached_submissions:
                previous, view = self.cached_submissions[call_id]
                if previous != payload:
                    raise ValueError("同一 tool_call_id 不能用于不同写入")
                return view
            material = request.material
            if any(target not in self.history for _, target in material.relations):
                raise ValueError(
                    "关系只能指向本批已完成历史文件，不能自关联、越界或指向未来文件"
                )
            # 只校验摘要显式引用的图片确实登记过，不替模型判断内容是否读懂。
            registered = {
                image.path
                for doc in [self.current, *self.history.values()]
                for section in doc.sections
                for block in section.blocks
                for image in block.images
            }
            paths = set(
                re.findall(
                    r"[A-Za-z0-9_-]+/images/[A-Za-z0-9_.-]+", material.evidence_summary
                )
            )
            if not paths <= registered:
                raise ValueError("详细摘要包含未登记的图片路径")
            record = EvidenceMaterial(
                **{
                    **material.model_dump(),
                    "relations": list(dict.fromkeys(material.relations)),
                },
                file_id=self.file_id,
            )
            records = [
                item for item in self.library.materials if item.file_id != self.file_id
            ] + [record]
            candidate = EvidenceLibrary(
                bidder_id=self.library.bidder_id,
                source_files=self.library.source_files,
                materials=records,
            )
            # 库文件是权威结果；写失败不发布候选 State，其他文件记录不变。
            write_library(candidate, self.settings)
            self.library = candidate
            self.unresolved_write_error = None
            view = render_material(candidate, self.file_id)
            self.cached_submissions[call_id] = (payload, view)
            return view


def build_evidence_ingestion_tools(session: EvidenceSession) -> list[StructuredTool]:
    async def save_evidence_material(
        material, file_ids, tool_call_id: Annotated[str, InjectedToolCallId]
    ):
        return await session.apply(
            SaveEvidenceMaterialInput(material=material, file_ids=file_ids),
            tool_call_id,
        )

    return [
        StructuredTool.from_function(
            coroutine=save_evidence_material,
            name="save_evidence_material",
            args_schema=SaveMaterialToolInput,
            description=load_tool_description("save_evidence_material"),
        )
    ]
