"""显式开启的真实材料全链路烟测；通过不等于审核质量达标。"""

import json
import os
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from tests.e2e.conftest import multipart_folder


def await_library(client, base, data, processing):
    deadline = time.monotonic() + int(os.getenv("MODEL_E2E_TIMEOUT", "1800"))
    while data["status"] in processing:
        assert time.monotonic() < deadline, "资料/规则库处理超时"
        time.sleep(1)
        response = client.get(base + "/progress")
        response.raise_for_status()
        data = response.json()
    return data


def test_competition_dataset_audit(competition_dataset):
    with TestClient(app) as client:
        files, data = multipart_folder(competition_dataset["tender"])
        data.update(client_request_id=str(uuid4()), display_name="比赛规则测试")
        project_response = client.post("/api/projects", files=files, data=data)
        project_response.raise_for_status()
        project = project_response.json()
        project = await_library(
            client,
            f"/api/projects/{project['project_id']}",
            project,
            {"processing_rules"},
        )
        assert project["status"] == "rule_ready"

        files, data = multipart_folder(competition_dataset["evidence"])
        data.update(client_request_id=str(uuid4()), display_name="比赛资料测试")
        bidder_response = client.post(
            "/api/bidders",
            files=files,
            data=data,
        )
        bidder_response.raise_for_status()
        bidder = bidder_response.json()
        bidder = await_library(
            client,
            f"/api/bidders/{bidder['bidder_id']}",
            bidder,
            {"uploading_evidence", "processing_evidence"},
        )
        assert bidder["status"] == "evidence_ready"
        detail = client.get(f"/api/bidders/{bidder['bidder_id']}")
        detail.raise_for_status()
        library = detail.json()["library"]
        source_ids = {item["file_id"] for item in library["source_files"]}
        assert source_ids == {item["file_id"] for item in library["materials"]}
        assert source_ids == set(bidder["completed_file_ids"])
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
        project_detail = client.get(f"/api/projects/{project['project_id']}")
        project_detail.raise_for_status()
        data.update(
            display_name=f"比赛审核-{uuid4().hex[:8]}",
            client_request_id=str(uuid4()),
            assignments=json.dumps(
                [
                    {
                        "upload_index": index,
                        "project_id": project["project_id"],
                        "bidder_id": bidder["bidder_id"],
                        "project_revision": project_detail.json()["revision"],
                        "bidder_revision": detail.json()["revision"],
                    }
                    for index in range(len(files))
                ]
            ),
        )
        task_response = client.post(
            "/api/audit-tasks",
            files=files,
            data=data,
        )
        task_response.raise_for_status()
        task_id = task_response.json()["task_id"]
        deadline = time.monotonic() + int(os.getenv("MODEL_E2E_TIMEOUT", "1800"))
        while True:
            response = client.get(f"/api/audit-tasks/{task_id}")
            response.raise_for_status()
            task = response.json()["task"]
            if task["status"] != "running":
                break
            assert time.monotonic() < deadline, f"审核超时：{task_id}"
            time.sleep(1)
        assert task["status"] == "completed", task.get("error")
        output = client.get(task["outputs"]["batch_json_url"])
        output.raise_for_status()
        result = output.json()
        assert result["schema_version"] == "3.0"
        assert all(item["error"] is None for item in result["files"])
        file_results = [item["result"] for item in result["files"]]
        assert len(file_results) == len(files)
        assert all(f["outputs"]["word_url"] for f in response.json()["files"])
        rules = {
            rule["rule_id"]
            for rule in result["rule_libraries"][project["project_id"]]["rules"]
        }
        for item in file_results:
            if item["is_applicable"]:
                assert {finding["rule_id"] for finding in item["findings"]} == rules
            else:
                assert item["exclusion_reason"] and not item["findings"]
        # 路由把所有文件排除也可能程序成功，不能算审核 Agent 已跑通。
        assert any(item["is_applicable"] for item in file_results)
