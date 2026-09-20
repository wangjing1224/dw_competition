"""供后续 view_image 工具复用的消息构造，不绑定 Agent，不调用模型。"""

from langchain_core.messages import ToolMessage

from app.models.documents import ParsedDocument
from app.parsers.assets import AssetStore, build_image_content


def build_view_image_result(
    *,
    parsed: ParsedDocument,
    image_path: str,
    assets: AssetStore,
    allowed_file_ids: set[str],
    tool_call_id: str,
    max_side: int = 2048,
) -> ToolMessage:
    # allowed_file_ids 必须由业务 state 注入，不能作为模型可填写的工具参数。
    if parsed.document.file_id not in allowed_file_ids:
        raise ValueError("图片文件不在当前任务授权范围")
    if not tool_call_id.strip():
        raise ValueError("缺失实际工具调用 ID")
    return ToolMessage(
        tool_call_id=tool_call_id,
        content=[
            {
                "type": "text",
                "text": f"文件 {parsed.document.file_id} 的材料图片：{image_path}",
            },
            build_image_content(parsed, image_path, assets, max_side=max_side),
        ],
    )
