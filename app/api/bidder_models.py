"""管理 DTO：不接受模型参数、人工来源标记和存储路径。"""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from app.models.common import StrictModel
from app.models.evidence import EvidenceMaterialInput


class Rename(StrictModel):
    display_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]


class MaterialEdit(EvidenceMaterialInput):
    file_id: str


class RenameAction(StrictModel):
    action: Literal["rename"]
    data: Rename


class UpdateMaterialAction(StrictModel):
    action: Literal["update_material"]
    data: MaterialEdit


BidderEdit = Annotated[
    RenameAction | UpdateMaterialAction, Field(discriminator="action")
]
