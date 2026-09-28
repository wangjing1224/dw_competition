"""固定库、解析和来源权限。业务数据只通过确定性 Markdown 投影进入模型。"""

from app.evidence.context import related_file_ids
from app.models.documents import document_ref_from_record
from app.parsers.service import load_parsed_document
from app.retrieval.chunker import chunk_parsed_document
from app.retrieval.index import embedding_config_fingerprint
from app.retrieval.models import IndexFilter
from app.retrieval.store import load_index


def brief_catalog(library):
    return (
        "\n\n".join(
            f"### {item.file_id}\n\n{item.bid_summary}" for item in library.materials
        )
        or "无资料"
    )


def evidence_view(library, starting_ids):
    known = {item.file_id: item for item in library.materials}
    if not starting_ids or not set(starting_ids) <= known.keys():
        raise ValueError("核验起始文件不属于当前资料库")
    selected = list(
        dict.fromkeys(
            [
                *starting_ids,
                *[
                    key
                    for start in starting_ids
                    for key in related_file_ids(library, start)
                ],
            ]
        )
    )
    relations = []
    for item in library.materials:
        for relation, target in item.relations:
            if (
                item.file_id in selected
                and target in selected
                and (item.file_id in starting_ids or target in starting_ids)
            ):
                relations.append(f"- {item.file_id} → {target}：{relation}")
    return (
        selected,
        "\n".join(relations) or "无直接关联",
        "\n\n".join(f"### {key}\n\n{known[key].evidence_summary}" for key in selected),
    )


def scope_for(document, overrides=None):
    role = str(document.data_role)
    scope_id = {
        "rule_source": f"rules-{document.project_id}",
        "evidence_material": f"evidence-{document.bidder_id}",
        "bid_document": f"bid-{document.task_id}",
    }[role]
    scope_id = (overrides or {}).get(document.file_id, scope_id)
    return IndexFilter(
        scope_id=scope_id,
        data_role=role,
        project_id=document.project_id,
        bidder_id=document.bidder_id,
        task_id=document.task_id,
    )


def fixed_parsed(source_files, repository, *, role, project_id=None, bidder_id=None):
    parsed = {}
    for source in source_files:
        document = document_ref_from_record(repository.require(source.file_id))
        if (
            document.data_role != role
            or document.project_id != project_id
            or document.bidder_id != bidder_id
            or document.task_id
        ):
            raise ValueError("库文件登记与业务归属不符")
        if (
            document.file_name != source.file_name
            or document.source_path != source.source_path
        ):
            raise ValueError("库文件快照与上传记录不符")
        value = load_parsed_document(source.file_id)
        if any(
            getattr(value.document, key) != getattr(document, key)
            for key in ("file_id", "data_role", "project_id", "bidder_id", "task_id")
        ):
            raise ValueError("解析文件身份与上传记录不一致")
        if value.schema_version != 2 or not value.markdown_path:
            raise ValueError("库文件缺少有效解析快照，请重新入库")
        parsed[source.file_id] = value.model_copy(deep=True)
    return parsed


def verify_indexes(documents, settings, overrides=None):
    groups = {}
    for parsed in documents.values():
        scope = scope_for(parsed.document, overrides)
        groups.setdefault(scope.scope_id, {})[parsed.document.file_id] = parsed
    indexes = {}
    for scope_id, files in groups.items():
        index = load_index(
            scope_id,
            expected_fingerprint=embedding_config_fingerprint(settings.embedding_model),
        )
        if index.manifest.dimension != settings.embedding_model.dimension:
            raise ValueError("索引向量维度与配置不一致")
        expected = {
            chunk.chunk_id: chunk
            for parsed in files.values()
            for chunk in chunk_parsed_document(
                parsed,
                max_tokens=settings.retrieval.chunk_tokens,
                overlap_tokens=settings.retrieval.chunk_overlap,
            )
        }
        actual = {
            chunk.chunk_id: chunk for chunk in index.chunks if chunk.file_id in files
        }
        if len(actual) != sum(chunk.file_id in files for chunk in index.chunks):
            raise ValueError("索引中存在重复片段 ID")
        if actual != expected:
            raise ValueError("索引片段与固定解析快照不一致，需要重建")
        for chunk in index.chunks:
            if chunk.file_id not in files:
                continue
            parsed = files[chunk.file_id]
            if chunk.parse_version != parsed.markdown_path or any(
                getattr(chunk, key) != getattr(parsed.document, key)
                for key in ("data_role", "project_id", "bidder_id", "task_id")
            ):
                raise ValueError("索引来源与解析/业务快照不一致，需要重建")
        indexes[scope_id] = index
    return indexes
