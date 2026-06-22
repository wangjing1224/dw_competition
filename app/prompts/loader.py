"""Markdown Prompt 和 YAML 工具说明的统一加载器。"""

from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any

import yaml


PROMPT_ROOT = Path(__file__).resolve().parent


@lru_cache(maxsize=16)
def load_prompt(name: str) -> str:
    path = PROMPT_ROOT / "templates" / f"{name}.md"
    return path.read_text(encoding="utf-8")


def render_prompt(name: str, **values: Any) -> str:
    serialized = {key: str(value) for key, value in values.items()}
    return Template(load_prompt(name)).safe_substitute(serialized)


@lru_cache(maxsize=1)
def _tool_descriptions() -> dict[str, str]:
    path = PROMPT_ROOT / "tool_descriptions.yaml"
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError("tool_descriptions.yaml 根节点必须是对象")
    return {str(key): str(description) for key, description in value.items()}


def load_tool_description(name: str) -> str:
    try:
        return _tool_descriptions()[name]
    except KeyError as exc:
        raise KeyError(f"缺少工具说明: {name}") from exc
