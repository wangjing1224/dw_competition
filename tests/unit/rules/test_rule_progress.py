"""正式规则保存后仍保留逐次提交记录，不删除分析依据。"""

from types import SimpleNamespace

import pytest

from app.models.rules import RuleCommitRecord, RuleExtractionProgress, RuleItem, RuleSet, RuleSourceFile
from app.rules.extractor import extract_rule_set, rule_restart_state
from app.services.project_service import ProjectService
from tests.unit.rules.test_rule_agent import parsed_file


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


@pytest.mark.asyncio
async def test_file_retry_rolls_back_partial_merge_profile_and_candidates(monkeypatch, temp_settings):
    docs = [parsed_file(i) for i in (1, 2, 3)]
    completed_rule = RuleItem(
        rule_id="rule-1", requirement="前一文件的规则", reason="原依据",
        category="technical", priority="important", verification="bid_only", provenance="ai_generated",
        sources=[RuleSourceFile.from_document(docs[0].document)],
    )
    snapshots, visits = [], []

    async def first_run(*, session, **kwargs):
        fid = session.current.document.file_id
        if fid == "file-1":
            session.rule_set.project_profile = "前一文件简介"
            session.rule_set.rules = [completed_rule.model_copy(deep=True)]
            session.commits.append(RuleCommitRecord(tool_call_id="first", before=[], after=[completed_rule]))
            return session.rule_set
        assert fid == "file-2"
        session.phase = "merge"
        session.candidate_draft = "失败文件的旧候选"
        session.batch_index = 2
        session.next_unit_index = 1
        session.file_model_calls = 7
        session.rule_set.project_profile = "部分合并的简介"
        changed = completed_rule.model_copy(update={"requirement": "失败文件的部分改写"})
        session.rule_set.rules = [changed]
        session.commits.append(RuleCommitRecord(tool_call_id="partial", before=[completed_rule], after=[changed]))
        await session.persist(session.rule_set, session.commits)
        raise TimeoutError("model timeout")

    monkeypatch.setattr("app.rules.extractor._run_file", first_run)
    def save(value):
        snapshots.append(value.model_copy(deep=True))
    with pytest.raises(TimeoutError):
        await extract_rule_set("project-1", docs, object(), retrieval_service=object(), settings=temp_settings, progress_callback=save)
    failed = snapshots[-1]
    assert failed.completed_file_ids == ["file-1"]
    assert failed.rule_set.project_profile == "部分合并的简介"
    assert failed.file_start_rule_set.project_profile == "前一文件简介"

    async def resumed(*, session, **kwargs):
        fid = session.current.document.file_id
        visits.append(fid)
        assert session.phase == "extract" and session.candidate_draft == ""
        assert session.next_unit_index == session.file_model_calls == session.model_calls == 0
        assert session.rule_set.project_profile == "前一文件简介"
        assert session.rule_set.rules == [completed_rule]
        assert [c.tool_call_id for c in session.commits] == ["first"]
        assert "file-1" in session.history
        return session.rule_set

    monkeypatch.setattr("app.rules.extractor._run_file", resumed)
    await extract_rule_set("project-1", docs, object(), retrieval_service=object(), settings=temp_settings, progress_callback=save, resume=failed)
    assert visits == ["file-2", "file-3"]
    assert snapshots[-1].completed_file_ids == ["file-1", "file-2", "file-3"]
    assert failed.rule_set.project_profile == "部分合并的简介"  # 不原地污染失败快照
    assert snapshots[-1].error is None


def legacy_progress():
    docs = [parsed_file(1), parsed_file(2)]
    rules = RuleSet(project_id="project-1", source_files=[RuleSourceFile.from_document(p.document) for p in docs])
    progress = RuleExtractionProgress(
        project_id="project-1", document_index=2, document_count=2,
        current_file_id="file-2", completed_file_ids=["file-1"],
        parser_versions={p.document.file_id: p.markdown_path for p in docs},
        rule_set=rules, phase="extract", batch_index=1, model_calls=1,
    )
    return docs, progress


def test_legacy_first_extract_and_completed_file_can_resume():
    docs, progress = legacy_progress()
    assert rule_restart_state(progress, "project-1", docs)[0] == progress.rule_set
    progress.completed_file_ids.append("file-2")
    progress.phase = "merge"
    assert rule_restart_state(progress, "project-1", docs)[0] == progress.rule_set


@pytest.mark.parametrize("change", [
    {"phase": "merge"}, {"batch_index": 2}, {"next_unit_index": 1},
    {"parser_versions": {}}, {"completed_file_ids": ["file-2"]},
    {"current_file_id": "file-other"}, {"project_id": "project-other"},
])
def test_unsafe_progress_is_not_guessed(change):
    docs, progress = legacy_progress()
    with pytest.raises(ValueError):
        rule_restart_state(progress.model_copy(update=change), "project-1", docs)
