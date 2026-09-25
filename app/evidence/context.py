"""双摘要视图与阅读输入；只做确定性渲染，不再次调用模型聚合。"""

from app.models.evidence import EvidenceLibrary


def render_material(library: EvidenceLibrary, file_id: str, *, detailed=True) -> str:
    source = next(item for item in library.source_files if item.file_id == file_id)
    material = next(
        (item for item in library.materials if item.file_id == file_id), None
    )
    if material is None:
        return f"## {file_id}｜{source.file_name}\n\n尚无已保存结果"
    lines = [f"## {file_id}｜{source.file_name}", "简洁摘要：\n" + material.bid_summary]
    if detailed:
        lines.extend(
            [
                "详细摘要：\n" + material.evidence_summary,
                "该文件的已保存关系：\n"
                + (
                    "\n".join(
                        f"- 当前文件 → {target}：{relation}"
                        for relation, target in material.relations
                    )
                    or "无"
                ),
            ]
        )
    return "\n\n".join(lines)


def render_history(library, file_ids, *, detailed=False):
    return (
        "\n\n".join(
            render_material(library, key, detailed=detailed) for key in file_ids
        )
        or "暂无已完成历史资料"
    )


def related_file_ids(library: EvidenceLibrary, file_id: str) -> list[str]:
    """双向找到直接邻接文件；不反转关系原意，也不按自由关系词过滤。"""
    if not any(source.file_id == file_id for source in library.source_files):
        raise ValueError("资料文件未登记")
    neighbors = []
    for material in library.materials:
        for _, target in material.relations:
            if material.file_id == file_id:
                neighbors.append(target)
            elif target == file_id:
                neighbors.append(material.file_id)
    return list(dict.fromkeys(neighbors))
