"""SQLite 组合外键、状态更新和中断恢复集成测试。"""

import sqlite3

import pytest

from app.storage.database import initialize_database
from app.storage.repositories import (
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


def test_task_cannot_bind_bidder_from_another_project(isolated_database):
    projects = ProjectRepository()
    bidders = BidderRepository()
    tasks = AuditTaskRepository()
    project_a = projects.create()
    project_b = projects.create()
    bidder_b = bidders.create(project_b.project_id)

    with pytest.raises(Exception):
        tasks.create(project_a.project_id, bidder_b.bidder_id)


def test_interrupted_running_task_becomes_failed(isolated_database):
    project = ProjectRepository().create()
    bidder = BidderRepository().create(project.project_id)
    repository = AuditTaskRepository()
    task = repository.create(project.project_id, bidder.bidder_id)

    assert repository.mark_interrupted_tasks_failed() == 1
    recovered = repository.require(task.task_id)

    assert recovered.status == "failed"
    assert recovered.error_code == "process_interrupted"
