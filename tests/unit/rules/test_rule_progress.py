"""正式规则保存后仍保留逐次提交记录，不删除分析依据。"""

from types import SimpleNamespace

from app.models.rules import RuleExtractionProgress, RuleSet
from app.services.project_service import ProjectService


def test_progress_is_kept_after_final_rule_set_saved(temp_settings):
    service = ProjectService(temp_settings, SimpleNamespace())
    rule_set = RuleSet(project_id="project-1")
    progress = RuleExtractionProgress(
        project_id="project-1",
        document_index=1,
        document_count=1,
        current_file_id="file-1",
        completed_file_ids=["file-1"],
        model_calls=1,
        rule_set=rule_set,
    )
    service._write_rule_progress(progress)
    path = service._rule_progress_path("project-1")
    assert (
        RuleExtractionProgress.model_validate_json(path.read_text("utf-8")) == progress
    )
    service._write_rule_set(rule_set)
    assert path.exists()
