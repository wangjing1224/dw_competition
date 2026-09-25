"""显式开启的真实材料全链路烟测；通过不等于审核质量达标。"""

import os
import time

from fastapi.testclient import TestClient

from app.main import app
from tests.e2e.conftest import multipart_folder


def test_competition_dataset_audit(competition_dataset):
    with TestClient(app) as client:
        files, data = multipart_folder(competition_dataset["tender"])
        project_response = client.post("/api/projects", files=files, data=data)
        project_response.raise_for_status()
        project = project_response.json()
        assert project["status"] == "rule_ready"

        files, data = multipart_folder(competition_dataset["evidence"])
        bidder_response = client.post(
            "/api/bidders",
            files=files,
            data=data,
        )
        bidder_response.raise_for_status()
        bidder = bidder_response.json()
        assert bidder["status"] == "evidence_ready"
        detail = client.get(f"/api/bidders/{bidder['bidder_id']}")
        detail.raise_for_status()
        library = detail.json()["library"]
        source_ids = {item["file_id"] for item in library["source_files"]}
        assert source_ids == {item["file_id"] for item in library["materials"]}
        assert source_ids == set(detail.json()["progress"]["completed_file_ids"])
        assert len(source_ids) == len(
            [
                item
                for item in competition_dataset["evidence"].rglob("*")
                if item.is_file()
            ]
        )
        assert all(
            item["bid_summary"] and item["evidence_summary"]
            for item in library["materials"]
        )

        files, data = multipart_folder(competition_dataset["bids"])
        task_response = client.post(
            f"/api/projects/{project['project_id']}/bidders/"
            f"{bidder['bidder_id']}/audit-tasks",
            files=files,
            data=data,
        )
        task_response.raise_for_status()
        task_id = task_response.json()["task_id"]
        deadline = time.monotonic() + int(os.getenv("MODEL_E2E_TIMEOUT", "1800"))
        while True:
            response = client.get(f"/api/audit-tasks/{task_id}")
            response.raise_for_status()
            task = response.json()
            if task["status"] != "running":
                break
            assert time.monotonic() < deadline, f"审核超时：{task_id}"
            time.sleep(1)
        assert task["status"] == "completed", task.get("error")
        result = task["result"]
        assert result["schema_version"] == "2.0"
        assert not result["file_errors"]
        file_results = result["file_results"]
        assert len(file_results) == len(files)
        assert len(task["outputs"]["word"]) == len(files)
        rules = {rule["rule_id"] for rule in result["rules"]}
        for item in file_results:
            if item["is_applicable"]:
                assert {finding["rule_id"] for finding in item["findings"]} == rules
            else:
                assert item["exclusion_reason"] and not item["findings"]
        # 路由把所有文件排除也可能程序成功，不能算审核 Agent 已跑通。
        assert any(item["is_applicable"] for item in file_results)
