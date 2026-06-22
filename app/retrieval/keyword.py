"""精确编号保护、轻量倒排索引和关键词召回。"""

import re
from collections import defaultdict

from app.retrieval.models import IndexChunk


EXACT_PATTERN = re.compile(
    r"(?:[A-Za-z]{1,10}[-_/]?\d[\w./-]*)"
    r"|(?:\d+(?:\.\d+)?\s*(?:℃|°C|V|kV|A|mA|mm|cm|m|台|天|%))"
    r"|(?:\d{4}[-年/]\d{1,2}(?:[-月/]\d{1,2}日?)?)",
    re.IGNORECASE,
)
WORD_PATTERN = re.compile(r"[A-Za-z0-9_.\-/]+|[\u4e00-\u9fff]{2,}")


def keyword_tokens(text: str) -> list[str]:
    exact = [item.lower().replace(" ", "") for item in EXACT_PATTERN.findall(text)]
    words: list[str] = []
    for token in WORD_PATTERN.findall(text.lower()):
        words.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", token):
            words.extend(token[index : index + 2] for index in range(len(token) - 1))
    return list(dict.fromkeys([*exact, *words]))


def build_keyword_index(chunks: list[IndexChunk]) -> dict[str, list[int]]:
    inverted: dict[str, list[int]] = defaultdict(list)
    for row_index, chunk in enumerate(chunks):
        for token in keyword_tokens(f"{chunk.section}\n{chunk.content}"):
            inverted[token].append(row_index)
    return dict(inverted)


def keyword_top_k(
    query: str,
    chunks: list[IndexChunk],
    inverted: dict[str, list[int]],
    allowed_indices: set[int],
    k: int,
) -> list[tuple[int, float]]:
    scores: dict[int, float] = defaultdict(float)
    tokens = keyword_tokens(query)
    if not tokens:
        return []
    for token in tokens:
        for row_index in inverted.get(token, []):
            if row_index in allowed_indices:
                scores[row_index] += 1.0
    return sorted(
        ((index, score / len(tokens)) for index, score in scores.items()),
        key=lambda item: item[1],
        reverse=True,
    )[:k]
