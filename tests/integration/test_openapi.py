"""三个业务接口都进入 OpenAPI 的路由注册测试。"""

from fastapi.testclient import TestClient

from app.main import app


def test_business_routes_are_registered():
    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert "/api/projects" in paths
    assert "/api/projects/{project_id}/bidders" in paths
    assert (
        "/api/projects/{project_id}/bidders/{bidder_id}/audit-tasks" in paths
    )
    assert "/api/audit-tasks/{task_id}" in paths
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
    assert "智能招标审核" in overview.text
    assert "审核操作台" in workflow.text
    assert "任务观测" in monitor.text
