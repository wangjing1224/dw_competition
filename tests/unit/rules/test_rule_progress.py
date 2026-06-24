"""规则抽取进度文件原子写入和成功清理测试。"""

from types import SimpleNamespace

from app.models.documents import SourceRef
from app.models.rules import RuleExtractionProgress, RuleItem, RuleSet
from app.services.project_service import ProjectService


def test_rule_progress_is_kept_until_final_rule_set_is_written(temp_settings):
    service = ProjectService(temp_settings, SimpleNamespace())
    rule_set = RuleSet(
        project_id="project-1",
        rules=[
            RuleItem(
                rule_id="rule-1",
                category="technical",
                requirement="探测器分辨率不低于640x480",
                priority="critical",
                verification="bid_only",
                source=SourceRef(
                    file_id="file-1",
                    file_name="技术规范.md",
                    source_path="技术规范.md",
                ),
            )
        ],
    )
    progress = RuleExtractionProgress(
        project_id="project-1",
        document_index=1,
        document_count=2,
        batch_index=1,
        batch_count=3,
        source_file="技术规范.md",
        rule_count=1,
        rule_set=rule_set,
    )
    service._write_rule_progress(progress)
    progress_path = service._rule_progress_path("project-1")
    assert progress_path.exists()
    assert RuleExtractionProgress.model_validate_json(
        progress_path.read_text("utf-8")
    ).rule_count == 1

    service._write_rule_set(rule_set)
    assert not progress_path.exists()
