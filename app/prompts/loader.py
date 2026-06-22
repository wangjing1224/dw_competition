"""读取根目录 configs/prompts 下的 Prompt 和工具说明配置。"""

import json
from functools import lru_cache
from string import Template
from typing import Any

import yaml
from pydantic import BaseModel

from app.core.config import PROJECT_ROOT


# Prompt 属于运行配置，不与 Python 业务模块放在一起。统一从项目根目录
# 定位，保证 Uvicorn 从任意工作目录启动时都能读取同一套配置。
PROMPT_CONFIG_ROOT = PROJECT_ROOT / "configs" / "prompts"


@lru_cache(maxsize=16)
def load_prompt(name: str) -> str:
    """读取并缓存固定 Markdown Prompt 正文。"""

    path = PROMPT_CONFIG_ROOT / "templates" / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Prompt 配置不存在: {path}")
    return path.read_text(encoding="utf-8")


def _serialize_prompt_value(value: Any) -> str:
    """把运行时对象转换为模型容易读取的稳定 JSON 或字符串。"""

    if isinstance(value, BaseModel):
        return value.model_dump_json(indent=2)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value)


def render_prompt(name: str, **values: Any) -> str:
    """使用 `$name`/`${name}` 渲染；缺少占位符变量时立即失败。"""

    serialized = {
        key: _serialize_prompt_value(value) for key, value in values.items()
    }
    return Template(load_prompt(name)).substitute(serialized)


@lru_cache(maxsize=1)
def _tool_descriptions() -> dict[str, str]:
    path = PROMPT_CONFIG_ROOT / "tool_descriptions.yaml"
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError("tool_descriptions.yaml 根节点必须是对象")
    return {str(key): str(description) for key, description in value.items()}


def load_tool_description(name: str) -> str:
    try:
        return _tool_descriptions()[name]
    except KeyError as exc:
        raise KeyError(f"缺少工具说明: {name}") from exc
