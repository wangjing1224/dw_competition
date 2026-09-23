"""各 Agent 共用的工具提供类；每个会话单独注入文件范围和解析快照。"""

from typing import Annotated

from langchain_core.tools import InjectedToolCallId, StructuredTool
from pydantic import Field

from app.core.config import AppSettings
from app.models.common import StrictModel
from app.models.documents import ParsedDocument, validate_asset_path
from app.parsers.assets import AssetStore, file_asset_root
from app.parsers.image_messages import build_view_image_result
from app.prompts.loader import load_tool_description


class ViewImageInput(StrictModel):
    path: str = Field(
        description="输入或检索结果中的图片路径，如 file-2/images/1.png，原样传入，不填写磁盘绝对路径或 URL。"
    )
    tool_call_id: Annotated[str, InjectedToolCallId]


class CommonTool:
    def __init__(
        self, *, parsed_documents: dict[str, ParsedDocument], settings: AppSettings
    ):
        # 仅接收当前业务授权范围，不在这里推测 project/bidder/task 归属。
        self.parsed_documents = dict(parsed_documents)
        self.settings = settings

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
            return result

        return [
            StructuredTool.from_function(
                coroutine=view_image,
                name="view_image",
                args_schema=ViewImageInput,
                description=load_tool_description("view_image"),
            )
        ]
