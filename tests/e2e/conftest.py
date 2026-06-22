"""真实比赛数据 E2E 的目录发现和 multipart 上传辅助。"""

import os
from pathlib import Path

import pytest


@pytest.fixture
def competition_dataset():
    """只有显式开启时才调用真实模型，防止普通 pytest 产生费用。"""

    if os.getenv("RUN_MODEL_E2E") != "1":
        pytest.skip("设置 RUN_MODEL_E2E=1 后运行真实模型 E2E")
    root_value = os.getenv("COMPETITION_DATASET_ROOT")
    if not root_value:
        pytest.skip("未配置 COMPETITION_DATASET_ROOT")
    root = Path(root_value).expanduser().resolve()
    tender = root / "示例招标文件"
    evidence = root / "投标方资料"
    bids = root / "投标文件测试集"
    if not all(path.is_dir() for path in (tender, evidence, bids)):
        pytest.skip("比赛数据目录缺少示例招标文件、投标方资料或投标文件测试集")
    return {"tender": tender, "evidence": evidence, "bids": bids}


def multipart_folder(path: Path):
    files = []
    data = []
    for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
        files.append(
            (
                "files",
                (
                    file_path.name,
                    file_path.read_bytes(),
                    "application/octet-stream",
                ),
            )
        )
        data.append(("relative_paths", file_path.relative_to(path).as_posix()))
    return files, data
