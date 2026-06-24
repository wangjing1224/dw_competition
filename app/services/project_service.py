"""项目创建、招标文件解析、规则抽取和依据索引构建。"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from app.core.config import AppSettings
from app.llm.factory import ModelRegistry
from app.models.documents import document_ref_from_record
from app.models.rules import RuleSet
from app.parsers.service import parse_document
from app.retrieval.index import build_document_index
from app.retrieval.models import IndexScope
from app.rules.extractor import extract_rule_set
from app.rules.merger import merge_rule_set
from app.storage.file_store import FileOwner, FileStore
from app.storage.repositories import ProjectRecord, ProjectRepository


class ProjectService:
    """同步完成项目预处理；模型耗时较长时接口会等待结果。"""

    def __init__(
        self,
        settings: AppSettings,
        models: ModelRegistry,
        *,
        repository: ProjectRepository | None = None,
        file_store: FileStore | None = None,
    ) -> None:
        self.settings = settings
        self.models = models
        self.repository = repository or ProjectRepository()
        self.file_store = file_store or FileStore()

    async def create_project(
        self,
        uploads: list[tuple[str, str, BinaryIO]],
    ) -> ProjectRecord:
        project = self.repository.create()
        try:
            records = [
                self.file_store.save(
                    owner=FileOwner(project_id=project.project_id),
                    data_role="rule_source",
                    stream=stream,
                    file_name=file_name,
                    relative_path=relative_path,
                )
                for file_name, relative_path, stream in uploads
            ]
            documents = [document_ref_from_record(record) for record in records]
            parsed = [parse_document(document) for document in documents]
            if not any(item.sections for item in parsed):
                raise ValueError("招标文件没有可用于规则抽取的文本")

            await build_document_index(
                IndexScope(
                    scope_id=f"rules-{project.project_id}",
                    data_role="rule_source",
                    project_id=project.project_id,
                ),
                documents,
                self.models.embedding,
                self.settings,
            )
            extracted = await extract_rule_set(
                project.project_id,
                parsed,
                self.models.rule_extraction,
                self.settings.rule_extraction_model,
                maximum=self.settings.audit.rule_limit,
            )
            rule_set = merge_rule_set(
                extracted,
                limit=self.settings.audit.rule_limit,
            )
            if not rule_set.rules:
                raise ValueError("模型未抽取到任何审核规则")
            rule_path = self._write_rule_set(rule_set)
            return self.repository.update_status(
                project.project_id,
                "rule_ready",
                rule_set_path=self.file_store.relative_to_data_root(rule_path),
            )
        except Exception as exc:
            self.repository.update_status(
                project.project_id,
                "rule_failed",
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            raise

    def _write_rule_set(self, rule_set: RuleSet) -> Path:
        """RuleSet 使用临时文件校验后原子替换，避免半写入状态。"""

        directory = self.settings.storage.data_root / "parsed" / "rules"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{rule_set.project_id}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(rule_set.model_dump_json(indent=2), encoding="utf-8")
        RuleSet.model_validate_json(temporary.read_text("utf-8"))
        temporary.replace(path)
        return path
