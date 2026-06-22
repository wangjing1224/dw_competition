"""公共测试配置和临时运行目录 fixture。"""

from pathlib import Path

import pytest

from app.core.config import get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("APP_DEBUG", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def temp_settings(tmp_path: Path):
    """返回数据目录指向临时路径的完整配置，避免测试污染项目 data。"""

    settings = get_settings()
    return settings.model_copy(
        update={
            "storage": settings.storage.model_copy(
                update={
                    "data_root": tmp_path / "data",
                    "database_path": tmp_path / "data" / "app.db",
                }
            )
        }
    )
