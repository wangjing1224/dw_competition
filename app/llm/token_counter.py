"""文本计数器注册表。计数器与模型平台解耦；不加载生成模型、不在线下载。"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

logger = logging.getLogger(__name__)
TOKENIZER_ROOT = Path(__file__).resolve().parents[2] / "models/tokenizers"
# 官方 deepseek_v4_tokenizer.zip 中 tokenizer.json 的固定版本；不使用包内
# model_max_length 或 chat_template 推断网关能力，后者另由服务能力表管理。
# https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip
DEEPSEEK_SHA256 = "89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614"


class CounterUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class TokenCounter:
    name: str
    count_text: Callable[[str], int]
    image_tokens_per_image: int | None = None
    degradation_reason: str | None = None

    @property
    def signature(self):
        return (self.name, self.image_tokens_per_image, self.degradation_reason)

    def count_image(self) -> int:
        if not self.image_tokens_per_image or self.image_tokens_per_image < 0:
            raise CounterUnavailable("实际图像输入缺少已确认的视觉计数策略")
        return self.image_tokens_per_image


def build_utf8_counter() -> TokenCounter:
    return TokenCounter("utf8-bytes-v1", lambda text: len(text.encode("utf-8")))


@lru_cache(maxsize=1)
def build_deepseek_counter() -> TokenCounter:
    path = TOKENIZER_ROOT / "deepseek_v4_tokenizer/tokenizer.json"
    if not path.is_file():
        raise CounterUnavailable("缺少本地 DeepSeek 分词资源，安装说明见 README")
    if sha256(path.read_bytes()).hexdigest() != DEEPSEEK_SHA256:
        raise CounterUnavailable("DeepSeek 分词资源指纹与已核对版本不符")
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(path))
    # 直接使用词表后端，避免 AutoTokenizer 的模型自动推断及远程代码。
    tokenizer.no_truncation()
    tokenizer.no_padding()
    if not tokenizer.encode("Token 计数", add_special_tokens=False).ids:
        raise CounterUnavailable("分词器未返回有效 Token")
    return TokenCounter(
        f"DeepSeek:{DEEPSEEK_SHA256}",
        lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids),
    )


TOKENIZER_REGISTRY = {"DeepSeek": build_deepseek_counter, "utf8": build_utf8_counter}


def get_token_counter(name: str, *, image_tokens_per_image=None) -> TokenCounter:
    reason = None
    try:
        if name not in TOKENIZER_REGISTRY:
            raise CounterUnavailable("未注册的分词器名称")
        counter = TOKENIZER_REGISTRY[name]()
    except (CounterUnavailable, OSError, ImportError) as exc:
        reason = str(exc)
        logger.warning(
            "Token 计数降级：requested=%s actual=utf8 reason=%s", name, reason
        )
        counter = build_utf8_counter()
    return TokenCounter(
        counter.name, counter.count_text, image_tokens_per_image, reason
    )
