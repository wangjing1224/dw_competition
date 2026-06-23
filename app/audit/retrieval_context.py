"""按规则分类预检索投标响应，供主 Agent 一次审核全部规则。"""

from app.models.documents import DocumentRef
from app.models.rules import RuleItem, RuleSet
from app.retrieval.models import IndexFilter, SearchResult
from app.retrieval.service import RetrievalService


def group_rules(rule_set: RuleSet) -> dict[str, list[RuleItem]]:
    grouped: dict[str, list[RuleItem]] = {}
    for rule in rule_set.rules:
        grouped.setdefault(str(rule.category), []).append(rule)
    return grouped


def build_category_query(category: str, rules: list[RuleItem]) -> str:
    lines = [f"审核类别：{category}"]
    for rule in rules:
        lines.append(f"{rule.name}：{rule.requirement}")
        lines.extend(item.requirement for item in rule.check_items)
    return "\n".join(dict.fromkeys(lines))


async def build_grouped_retrieval_context(
    retrieval: RetrievalService,
    *,
    task_id: str,
    project_id: str,
    bidder_id: str,
    document: DocumentRef,
    rule_set: RuleSet,
) -> dict[str, list[SearchResult]]:
    context: dict[str, list[SearchResult]] = {}
    for category, rules in group_rules(rule_set).items():
        results = await retrieval.search(
            build_category_query(category, rules),
            IndexFilter(
                scope_id=f"bid-{task_id}",
                project_id=project_id,
                bidder_id=bidder_id,
                task_id=task_id,
                data_role="bid_document",
                file_ids=[document.file_id],
            ),
            candidate_top_k=20,
            final_top_k=5,
        )
        context[category] = [
            item.model_copy(
                update={"content": item.content[:1500]}
            )
            for item in results
        ]
    return context
