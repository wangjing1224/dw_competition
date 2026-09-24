"""三平台 Chat Completions 适配；消息转换同时供真实请求和输入计数使用。"""

from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import (
    _convert_from_v1_to_chat_completions,
    _convert_message_to_dict,
)

Platform = Literal["deepseek", "bailian", "openai"]

# 能力按具体型号登记，不由平台名猜测分词器，也不把所有 OpenAI 型号当推理模型。
# https://developers.openai.com/api/docs/models/gpt-5.1
OPENAI_SWITCHABLE_REASONING = {"gpt-5.1", "gpt-5.1-2025-11-13"}
OPENAI_ALWAYS_REASONING = {
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "o3",
    "o3-mini",
    "o4-mini",
}
# https://help.aliyun.com/zh/model-studio/deep-thinking （传递思考过程）
BAILIAN_PRESERVE_THINKING = {"qwen3.6-plus", "qwen3.6-plus-2026-04-02"}


@dataclass(frozen=True)
class ChatProtocol:
    platform: Platform = "openai"
    model: str = ""
    thinking: bool = False

    @classmethod
    def from_settings(cls, settings):
        return cls(settings.platform, settings.model, settings.enable_thinking)

    def generation_options(self, tokens: int | None = None) -> dict:
        """固定额度与控制器计算的额度走同一路径，不能覆盖掉平台思考开关。"""
        if self.platform == "deepseek":
            options = {
                "extra_body": {
                    "thinking": {"type": "enabled" if self.thinking else "disabled"}
                }
            }
        elif self.platform == "bailian":
            extra = {"enable_thinking": self.thinking}
            if self.model in BAILIAN_PRESERVE_THINKING:
                extra["preserve_thinking"] = self.thinking
            options = {"extra_body": extra}
        elif self.platform == "openai":
            options = {}
            if self.model in OPENAI_SWITCHABLE_REASONING:
                options["reasoning_effort"] = "medium" if self.thinking else "none"
            elif self.model in OPENAI_ALWAYS_REASONING:
                if not self.thinking:
                    raise ValueError(
                        f"{self.model} 不支持关闭思考，请换用可关闭的型号或开启思考"
                    )
                options["reasoning_effort"] = "medium"
            elif self.thinking or self.model.startswith(("gpt-5", "o1", "o3", "o4")):
                raise ValueError(
                    f"尚未登记 {self.model} 的思考参数，请核实后补充平台能力表"
                )
        else:
            raise ValueError(f"不支持的平台：{self.platform}")
        if tokens is not None:
            # 百炼的新参数同样包含思维链与回答；旧 max_tokens 只限制回答，
            # 不能用来落实控制器给出的总输出额度。
            options[
                "max_tokens" if self.platform == "deepseek" else "max_completion_tokens"
            ] = tokens
        return options

    def wire_messages(self, messages, *, has_tools: bool) -> list[dict]:
        # 同时兼容 LangChain v0/v1 消息格式；不重新实现其工具参数/图像块转换。
        wire = [
            _convert_message_to_dict(
                _convert_from_v1_to_chat_completions(message)
                if isinstance(message, AIMessage)
                else message
            )
            for message in messages
        ]
        replay_reasoning = self.thinking and (
            (self.platform == "deepseek" and has_tools)
            or (self.platform == "bailian" and self.model in BAILIAN_PRESERVE_THINKING)
        )
        for original, item in zip(messages, wire, strict=True):
            if item["role"] == "assistant" and replay_reasoning:
                # DS 思考+工具模式要求回传；无工具收尾则不占用下一次输入。
                item["reasoning_content"] = (
                    original.additional_kwargs.get("reasoning_content") or ""
                )
            if (
                self.platform == "openai"
                and self.model.startswith(("o1", "o3", "o4"))
                and item["role"] == "system"
            ):
                item["role"] = "developer"
        return wire


class PlatformChatOpenAI(ChatOpenAI):
    """保留第三方公开响应字段；网络、工具绑定、usage 处理仍复用 LangChain。"""

    protocol: ChatProtocol

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        payload["messages"] = self.protocol.wire_messages(
            self._convert_input(input_).to_messages(),
            has_tools=bool(payload.get("tools")),
        )
        if self.protocol.platform == "deepseek" and "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        return payload

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        if self.protocol.platform != "openai":
            raw = response if isinstance(response, dict) else response.model_dump()
            for generation, choice in zip(
                result.generations, raw["choices"], strict=True
            ):
                reasoning = choice["message"].get("reasoning_content")
                if isinstance(reasoning, str):
                    generation.message.additional_kwargs["reasoning_content"] = (
                        reasoning
                    )
        return result

    def _convert_chunk_to_generation_chunk(
        self, chunk, default_chunk_class, base_generation_info
    ):
        result = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if (
            result is not None
            and self.protocol.platform != "openai"
            and chunk.get("choices")
        ):
            reasoning = (chunk["choices"][0].get("delta") or {}).get(
                "reasoning_content"
            )
            if isinstance(reasoning, str):
                result.message.additional_kwargs["reasoning_content"] = reasoning
        return result
