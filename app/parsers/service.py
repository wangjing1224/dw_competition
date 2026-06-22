"""统一选择解析器、持久化解析 JSON 并更新文件状态。"""

from __future__ import annotations

from pathlib import Path

from app.core.config import get_settings
from app.models.documents import DocumentRef, ParsedDocument
from app.parsers.base import RecoverableParseError
from app.parsers.registry import ParserRegistry, parser_registry
from app.storage.file_store import FileStore
from app.storage.repositories import FileRepository


def _parsed_path(file_id: str) -> Path:
    return get_settings().storage.data_root / "parsed" / f"{file_id}.json"


def persist_parse_result(
    result: ParsedDocument,
    repository: FileRepository | None = None,
) -> ParsedDocument:
    path = _parsed_path(result.document.file_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    ParsedDocument.model_validate_json(temporary.read_text(encoding="utf-8"))
    temporary.replace(path)
    (repository or FileRepository()).update_parse_status(
        result.document.file_id, result.parse_status
    )
    return result


def load_parsed_document(file_id: str) -> ParsedDocument:
    return ParsedDocument.model_validate_json(
        _parsed_path(file_id).read_text(encoding="utf-8")
    )


def parse_document(
    document: DocumentRef,
    *,
    file_store: FileStore | None = None,
    registry: ParserRegistry | None = None,
    repository: FileRepository | None = None,
) -> ParsedDocument:
    store = file_store or FileStore(repository)
    selected_registry = registry or parser_registry
    stored_path = store.resolve(document.file_id)
    parser = selected_registry.get(stored_path.suffix.lower())
    if parser is None:
        return persist_parse_result(
            ParsedDocument(
                document=document.model_copy(update={"parse_status": "unsupported"}),
                parse_status="unsupported",
                warnings=["当前格式没有可用解析器"],
            ),
            repository,
        )
    try:
        return persist_parse_result(
            parser.parse(document, stored_path), repository
        )
    except RecoverableParseError as exc:
        partial = exc.partial_result.model_copy(
            update={
                "parse_status": "partial",
                "document": exc.partial_result.document.model_copy(
                    update={"parse_status": "partial"}
                ),
            }
        )
        return persist_parse_result(partial, repository)
    except Exception as exc:
        return persist_parse_result(
            ParsedDocument(
                document=document.model_copy(
                    update={"parse_status": "manual_review"}
                ),
                parse_status="manual_review",
                warnings=[f"解析失败: {type(exc).__name__}"],
            ),
            repository,
        )
