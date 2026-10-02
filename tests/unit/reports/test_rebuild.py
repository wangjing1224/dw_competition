"""仅修复已读完文件的报告，不用再次调用模型，也不能跳过未读范围。"""

import json

import pytest

from app.parsers.assets import atomic_write
from app.reports.rebuild import rebuild_result
from app.reports.service import ReportService
from app.storage.file_store import FileStore
from tests.helpers.audit import audit_context, filled_report


def save_snapshot(store, *, complete=True):
    context = audit_context()
    directory = store.task_output_dir(context.task_id)
    atomic_write(directory / "audit_context.json", context.model_dump_json().encode())
    atomic_write(
        directory / "drafts" / "bid-1.md", filled_report(context.rule_set).encode()
    )
    state = {
        "file_id": "bid-1",
        "total_units": 1,
        "next_unit_index": 1 if complete else 0,
        "sources": {},
    }
    atomic_write(directory / "drafts" / "bid-1.state.json", json.dumps(state).encode())
    return context, directory


@pytest.mark.parametrize("complete", [True, False])
def test_rebuild_respects_reading_completion(monkeypatch, temp_settings, complete):
    monkeypatch.setattr("app.storage.file_store.get_settings", lambda: temp_settings)
    store = FileStore()
    _, directory = save_snapshot(store, complete=complete)
    result = rebuild_result("audit-1", store)
    assert bool(result.file_errors) is not complete
    if complete:
        assert result.file_results[0].findings[0].rule_id == "rule-1"
        assert (directory / "bid-1_result.json").exists()
    else:
        assert "未完成的阅读范围" in result.file_errors["bid-1"]
        assert not result.file_results


def test_rebuild_reports_invalid_draft_then_accepts_repair(monkeypatch, temp_settings):
    from types import SimpleNamespace

    monkeypatch.setattr("app.storage.file_store.get_settings", lambda: temp_settings)
    store = FileStore()
    context, directory = save_snapshot(store)
    path = directory / "drafts" / "bid-1.md"
    atomic_write(
        path, (filled_report(context.rule_set) + "投标片段：invented\n").encode()
    )
    assert "bid-1" in rebuild_result("audit-1", store).file_errors
    atomic_write(path, filled_report(context.rule_set).encode())
    transitions = []
    repository = SimpleNamespace(
        require=lambda task_id: SimpleNamespace(status="failed"),
        transition=lambda *args, **kwargs: transitions.append((args, kwargs)),
    )
    paths = ReportService(repository, store).rebuild_from_markdown("audit-1")
    assert len(paths) == 1 and store.resolve_relative(paths[0]).exists()
    assert transitions[0][0] == ("audit-1", "failed", "completed")
    assert transitions[0][1]["error_code"] == ""


def test_rebuild_refuses_running_task():
    from types import SimpleNamespace

    repository = SimpleNamespace(
        require=lambda task_id: SimpleNamespace(status="running")
    )
    with pytest.raises(ValueError, match="运行中的任务"):
        ReportService(repository, object()).rebuild_from_markdown("audit-1")


def test_v2_rebuild_keeps_first_finalization_and_frozen_names(monkeypatch, temp_settings):
    from app.models.results import ReportContext
    from app.reports.presentation import report_markdown

    monkeypatch.setattr("app.storage.file_store.get_settings", lambda: temp_settings)
    store = FileStore()
    context, directory = save_snapshot(store)
    context.report_format_version = 2
    context.report_context = ReportContext(task_name="受理时名称", rule_library_name="原规则库", evidence_library_name="原资料库")
    atomic_write(directory / "audit_context.json", context.model_dump_json().encode())
    atomic_write(directory / "drafts" / "bid-1.md", filled_report(context.rule_set, version=2).encode())
    first = rebuild_result("audit-1", store)
    second = rebuild_result("audit-1", store)
    assert not first.file_errors and not second.file_errors
    assert first.file_results[0].report_context.finalized_at
    assert first.file_results[0].report_context == second.file_results[0].report_context
    assert report_markdown(first, first.file_results[0]) == report_markdown(second, second.file_results[0])
