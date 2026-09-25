"""三个业务接口都进入 OpenAPI 的路由注册测试。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def isolated_startup(monkeypatch, temp_settings):
    monkeypatch.setattr("app.main.get_settings", lambda: temp_settings)
    monkeypatch.setattr(
        "app.storage.database.database_path",
        lambda: temp_settings.storage.database_path,
    )


def test_business_routes_are_registered():
    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert "/api/projects" in paths
    assert "/api/bidders" in paths
    assert "/api/bidders/{bidder_id}" in paths
    assert "/api/projects/{project_id}/bidders/{bidder_id}/audit-tasks" in paths
    assert "/api/audit-tasks/{task_id}" in paths
    assert "/api/audit-tasks/{task_id}/stream" in paths
    assert "/api/audit-tasks/{task_id}/outputs/json" in paths
    assert "/api/audit-tasks/{task_id}/outputs/word/{output_index}" in paths


def test_demo_web_pages_are_served():
    with TestClient(app) as client:
        home = client.get("/", follow_redirects=False)
        overview = client.get("/web/")
        workflow = client.get("/web/workflow.html")
        monitor = client.get("/web/monitor.html")

    assert home.status_code in {302, 307}
    assert overview.status_code == 200
    assert "电力智能招标审核系统" in overview.text
    assert "电力智能招标审核系统" in workflow.text
    assert "电力智能招标审核系统" in monitor.text
    assert "审核操作台" in workflow.text
    assert "任务观测" in monitor.text
