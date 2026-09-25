"""SQLite 组合外键、状态更新和中断恢复集成测试。"""

import pytest

from app.storage.database import initialize_database
from app.storage.repositories import (
    AuditEventRepository,
    AuditTaskRepository,
    BidderRepository,
    ProjectRepository,
)


@pytest.fixture
def isolated_database(monkeypatch, tmp_path):
    path = tmp_path / "app.db"
    monkeypatch.setattr("app.storage.database.database_path", lambda: path)
    initialize_database(path)
    return path


def test_independent_bidder_can_be_selected_for_different_projects(isolated_database):
    projects = ProjectRepository()
    bidders = BidderRepository()
    tasks = AuditTaskRepository()
    project_a = projects.create()
    project_b = projects.create()
    bidder_b = bidders.create()

    assert project_a.project_id == "project-1"
    assert project_b.project_id == "project-2"
    assert bidder_b.bidder_id == "bidder-1"

    assert (
        tasks.create(project_a.project_id, bidder_b.bidder_id).project_id
        == project_a.project_id
    )
    assert (
        tasks.create(project_b.project_id, bidder_b.bidder_id).bidder_id
        == bidder_b.bidder_id
    )
    with pytest.raises(Exception):
        tasks.create("project-missing", bidder_b.bidder_id)


def test_interrupted_running_task_becomes_failed(isolated_database):
    project = ProjectRepository().create()
    bidder = BidderRepository().create()
    repository = AuditTaskRepository()
    task = repository.create(project.project_id, bidder.bidder_id)

    assert repository.mark_interrupted_tasks_failed() == 1
    recovered = repository.require(task.task_id)

    assert recovered.status == "failed"
    assert recovered.error_code == "process_interrupted"


def test_audit_events_are_ordered_by_append_sequence(isolated_database):
    project = ProjectRepository().create()
    bidder = BidderRepository().create()
    task = AuditTaskRepository().create(project.project_id, bidder.bidder_id)
    events = AuditEventRepository()

    events.append(
        task.task_id,
        actor="main_agent",
        event_type="agent_turn_started",
        title="开始分析",
    )
    events.append(
        task.task_id,
        actor="tool",
        event_type="tool_result",
        title="检索完成",
        detail={"result_count": 3},
    )

    loaded = events.list_by_task(task.task_id)

    assert [event.title for event in loaded] == ["开始分析", "检索完成"]
    assert loaded[1].detail == {"result_count": 3}
