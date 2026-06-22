"""相对路径规范化和路径穿越测试。"""

import pytest

from app.core.errors import BusinessError
from app.storage.file_store import normalize_relative_path


@pytest.mark.parametrize(
    "value",
    ["../secret.txt", "/etc/passwd", r"..\\secret.txt", ""],
)
def test_unsafe_relative_path_is_rejected(value):
    with pytest.raises(BusinessError):
        normalize_relative_path(value)


def test_normal_relative_path_is_preserved():
    assert normalize_relative_path("资料/合同.docx") == "资料/合同.docx"
