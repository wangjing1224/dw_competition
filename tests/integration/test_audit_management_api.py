"""真实解析/索引/工具/报告链路，模型用确定性替身；所有资产在临时目录。"""

import asyncio
import json
import re
import sqlite3
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.api.audit_tasks import router
from app.core.errors import register_exception_handlers
from app.evidence.storage import write_library
from app.models.documents import document_ref_from_record
from app.models.evidence import EvidenceLibrary, EvidenceMaterial, EvidenceSourceFile
from app.models.rules import RuleItem, RuleSet, RuleSourceFile
from app.parsers.service import prepare_document
from app.retrieval.index import build_document_index
from app.retrieval.models import IndexScope
from app.services.audit_batch_runner import run_audit_batch
from app.services.audit_management_service import AuditManagementService
from app.services.evidence_library_service import EvidenceLibraryService
from app.services.rule_library_service import RuleLibraryService
from app.storage.audit_management import file_rows, interrupt_tasks, update_file
from app.storage.database import initialize_database, open_db
from app.storage.file_store import FileOwner, FileStore
from app.storage.repositories import (
    BidderRepository,
    ProjectRepository,
)
from tests.helpers.audit import filled_report, tool_response


class Embedding:
    def __init__(self, dimension):
        self.dimension = dimension

    async def embed(self, texts):
        return np.ones((len(texts), self.dimension), dtype=np.float32)

    async def embed_query(self, text):
        return np.ones(self.dimension, dtype=np.float32)


@pytest.fixture
def env(monkeypatch, temp_settings):
    for module in (
        "app.storage.file_store",
        "app.parsers.service",
        "app.parsers.common",
        "app.retrieval.store",
        "app.services.evidence_library_service",
        "app.services.rule_library_service",
        "app.services.audit_management_service",
        "app.services.audit_batch_runner",
    ):
        monkeypatch.setattr(module + ".get_settings", lambda: temp_settings)
    monkeypatch.setattr(
        "app.storage.database.database_path",
        lambda: temp_settings.storage.database_path,
    )
    initialize_database(temp_settings.storage.database_path)
    queued = []
    monkeypatch.setattr(
        "app.api.audit_tasks.background_runner.submit",
        lambda fn, *args: queued.append((fn, args)),
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    service = AuditManagementService()
    embedding = Embedding(temp_settings.embedding_model.dimension)
    assignments, sources, rulesets = [], [], {}
    for index in range(2):
        project = ProjectRepository().create()
        bidder = BidderRepository().create()
        refs = []
        for role in ("rule_source", "evidence_material"):
            owner = (
                FileOwner(project_id=project.project_id)
                if role == "rule_source"
                else FileOwner(bidder_id=bidder.bidder_id)
            )
            record = FileStore().save(
                owner=owner,
                data_role=role,
                stream=BytesIO("项目要求：精度至少100；资料证明精度100。".encode()),
                file_name="sample.md",
                relative_path=f"{role}.md",
            )
            ref = document_ref_from_record(record)
            asyncio.run(prepare_document(ref))
            scope = IndexScope(
                scope_id=f"rules-{project.project_id}"
                if role == "rule_source"
                else f"evidence-{bidder.bidder_id}",
                data_role=role,
                project_id=owner.project_id,
                bidder_id=owner.bidder_id,
            )
            asyncio.run(build_document_index(scope, [ref], embedding, temp_settings))
            refs.append(ref)
        source = RuleSourceFile.from_document(refs[0])
        rules = RuleSet(
            project_id=project.project_id,
            project_profile="精度设备采购",
            source_files=[source],
            rules=[
                RuleItem(
                    rule_id="rule-1",
                    provenance="ai_generated",
                    category="technical",
                    requirement="精度至少100",
                    reason="依据技术表",
                    priority="critical",
                    verification="bid_only",
                    sources=[source],
                )
            ],
        )
        path = service.store.task_output_dir(project.project_id) / "rules.json"
        path.write_text(rules.model_dump_json())
        ProjectRepository().update_status(
            project.project_id,
            "rule_ready",
            rule_set_path=service.store.relative_to_data_root(path),
        )
        lib = EvidenceLibrary(
            bidder_id=bidder.bidder_id,
            source_files=[
                EvidenceSourceFile(
                    **refs[1].model_dump(
                        include={"file_id", "file_name", "source_path"}
                    )
                )
            ],
            materials=[
                EvidenceMaterial(
                    file_id=refs[1].file_id,
                    bid_summary="精度证明",
                    evidence_summary="证明精度100",
                    relations=[],
                )
            ],
        )
        write_library(lib, temp_settings)
        BidderRepository().update_status(bidder.bidder_id, "evidence_ready")
        p = RuleLibraryService().detail(project.project_id)
        b = EvidenceLibraryService().detail(bidder.bidder_id)
        assert p["available_for_audit"], p
        assert b["available_for_audit"], b
        assignments.append(
            {
                "upload_index": index,
                "project_id": project.project_id,
                "bidder_id": bidder.bidder_id,
                "project_revision": p["revision"],
                "bidder_revision": b["revision"],
            }
        )
        sources.append([r.file_id for r in refs])
        rulesets[project.project_id] = rules
    return SimpleNamespace(
        client=TestClient(app, raise_server_exceptions=True),
        service=service,
        settings=temp_settings,
        queued=queued,
        assignments=assignments,
        sources=sources,
        rules=rulesets,
        embedding=embedding,
    )


def upload(
    env, *, request_id=None, name="混合审核", assignments=None, content=b"precision 100"
):
    return env.client.post(
        "/api/audit-tasks",
        data={
            "display_name": name,
            "client_request_id": request_id or str(uuid4()),
            "assignments": json.dumps(
                assignments if assignments is not None else env.assignments
            ),
            "relative_paths": ["A/bid.md", "B/bid.md"],
        },
        files=[("files", ("bid.md", content)), ("files", ("bid.md", content))],
    )


def test_mixed_bindings_idempotency_scope_and_read_only(env):
    request_id = str(uuid4())
    first = upload(env, request_id=request_id)
    assert first.status_code == 202, first.text
    task = first.json()["task_id"]
    again = upload(env, request_id=request_id)
    assert again.status_code == 200 and again.json()["reused"]
    assert len(env.queued) == 1
    assert upload(env, request_id=request_id, content=b"different").status_code == 409
    assert upload(env).status_code == 409  # 同名任务
    detail = env.client.get(f"/api/audit-tasks/{task}").json()
    assert len(detail["files"]) == 2 and detail["task"]["project_count"] == 2
    for i, f in enumerate(detail["files"]):
        assert f["project_id"] == env.assignments[i]["project_id"]
        base = f"/api/audit-tasks/{task}/files/{f['file_id']}/sources"
        assert (
            env.client.get(f"{base}/{env.sources[1 - i][0]}/preview").status_code == 404
        )
        assert env.client.get(f"{base}/{env.sources[i][0]}/preview").status_code == 200
        assert env.client.get(f"{base}/{f['file_id']}/download").status_code == 200
        assert env.client.get(f"{base}/{f['file_id']}/preview").status_code == 409
    assert (
        env.client.get(f"/api/audit-tasks?client_request_id={request_id}").json()[
            "request_state"
        ]
        == "accepted"
    )
    assert (
        env.client.get(f"/api/audit-tasks/{task}/progress?include_trace=true").json()[
            "trace_updates"
        ]
        == []
    )
    assert (
        env.client.get(f"/api/audit-tasks/{task}/outputs?format=json").status_code
        == 409
    )
    first_file = detail["files"][0]["file_id"]
    preview_url = f"/api/audit-tasks/{task}/files/{first_file}/sources/{env.sources[0][0]}/preview"
    assert env.client.get(preview_url + "?version=not-this-snapshot").status_code == 404
    assert env.client.get(preview_url + "?image_name=1.png").status_code == 422
    with open_db() as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE audit_task_files SET project_id='project-2' WHERE task_id=?",
            (task,),
        )


def test_stale_revision_and_incomplete_bindings_rejected_before_creation(env):
    assignments = [dict(a) for a in env.assignments]
    assignments[0]["project_revision"] = "old"
    assert upload(env, assignments=assignments).status_code == 409
    assert upload(env, assignments=assignments[:1]).status_code == 422
    assert not env.queued
    assert env.client.get("/api/audit-tasks").json()["total"] == 0


class RouteModel:
    async def ainvoke(self, messages, **kwargs):
        return AIMessage(
            content='{"match_status":"matched","match_reason":"采购对象一致"}'
        )


class ReportModel:
    def __init__(self, rules, failure=None):
        self.rules, self.failure = rules, failure

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, **kwargs):
        if self.failure and self.failure in messages[1].content:
            raise RuntimeError("模型不可用 sk-secret12345 /Users/private/secret")
        if any(m.type == "tool" for m in messages):
            return AIMessage(content="处理完毕")
        return tool_response(
            "report_document",
            {
                "action": "write",
                "content": filled_report(self.rules, version=2),
                "edits": None,
            },
        )


@pytest.mark.asyncio
async def test_real_pipeline_per_file_results_traces_and_word_error(env, monkeypatch):
    creation = upload(env).json()
    task = creation["task_id"]
    files = [r["file_id"] for r in file_rows(task)]
    # 提交后编辑当前规则库；本次运行和报告必须仍读受理时的冻结版本。
    current = ProjectRepository().require("project-1")
    changed = env.rules["project-1"].model_copy(deep=True)
    changed.rules[0].requirement = "后续人工修改的要求"
    env.service.store.resolve_relative(current.rule_set_path).write_text(
        changed.model_dump_json()
    )
    from app.reports.word_report import generate_word_reports

    def exporter(path, store):
        if files[0] in path:
            raise RuntimeError("Word 渲染失败")
        return generate_word_reports(path, store)

    monkeypatch.setattr(
        "app.services.audit_batch_runner.generate_word_reports", exporter
    )
    models = SimpleNamespace(
        bid_summary=RouteModel(),
        bid_chat=ReportModel(env.rules["project-1"]),
        embedding=env.embedding,
        rerank=object(),
        evidence_chat=object(),
    )
    await run_audit_batch(task, models=models)
    detail = env.service.detail(task)
    assert detail["task"]["status"] == "completed_with_report_error", detail
    assert [f["status"] for f in detail["files"]] == ["completed", "completed"]
    for f in detail["files"]:
        report = env.service.report(task, f["file_id"])
        assert report["counts"] == {
            "compliant": 1,
            "non_compliant": 0,
            "risk": 0,
            "manual_review": 0,
            "total": 1,
        }
        assert report["document"]["kind"] == "final"
        assert report["result"]["report_title"] == "设备采购投标文件审核报告"
        assert report["rules"][0]["requirement"] == "精度至少100"
        assert env.service.output(task, "md", f["file_id"]).is_file()
        assert report["result"]["report_format_version"] == 2
        context = report["result"]["report_context"]
        assert context["task_name"] == "混合审核" and context["finalized_at"]
        assert context["rule_library_name"] == f["project_name"]
        assert context["evidence_library_name"] == f["bidder_name"]
        assert isinstance(report["document"]["revision"], str)
        assert len(report["document"]["revision"]) == 64
        assert "### rule-1" in report["document"]["markdown"]
        formal = env.service.output(task, "md", f["file_id"])
        assert formal.parent.name == "reports"
        assert formal.read_text() == report["document"]["display_markdown"]
        assert "rule-1" not in formal.read_text()
        assert "## 四、审核明细及原文摘录" in formal.read_text()
        assert (
            env.service.report(task, f["file_id"])["result"]["report_context"]
            == context
        )
    batch = json.loads(env.service.output(task, "json").read_bytes())
    assert batch["schema_version"] == "3.0" and len(batch["rule_libraries"]) == 2
    progress = env.service.progress(task, True, files[0], limit=2)
    assert progress["has_more"] and progress["activity"]["model_calls"] == 3
    updates = []
    cursor = 0
    while True:
        p = env.service.progress(task, True, files[0], cursor, 2)
        updates.extend(p["trace_updates"])
        cursor = p["next_seq"]
        if not p["has_more"]:
            break
    assert all(u["span"]["file_id"] == files[0] for u in updates)
    assert progress["activity"]["report_saves"] == 1
    final = {u["span"]["span_id"]: u["span"] for u in updates}
    assert not any(n["status"] == "running" for n in final.values())
    assert any(n["kind"] == "model" and n["parent_span_id"] for n in final.values())
    assert (
        env.client.get(
            f"/api/audit-tasks/{task}/outputs?format=word&file_id={files[0]}"
        ).status_code
        == 409
    )
    assert (
        env.client.get(
            f"/api/audit-tasks/{task}/outputs?format=word&file_id={files[1]}"
        ).status_code
        == 200
    )


@pytest.mark.asyncio
async def test_one_failed_file_does_not_prevent_other_file(env):
    task = upload(env).json()["task_id"]
    files = [r["file_id"] for r in file_rows(task)]
    models = SimpleNamespace(
        bid_summary=RouteModel(),
        bid_chat=ReportModel(env.rules["project-1"], failure=files[0]),
        embedding=env.embedding,
        rerank=object(),
        evidence_chat=object(),
    )
    await run_audit_batch(task, models=models)
    detail = env.service.detail(task)
    assert detail["task"]["status"] == "partially_failed"
    assert (
        detail["files"][0]["status"] == "failed"
        and detail["files"][1]["status"] == "completed"
    )
    assert "sk-secret" not in json.dumps(detail)
    assert "/Users/" not in json.dumps(detail)
    assert env.service.report(task, files[0])["counts"] is None


def test_restart_marks_active_stages_and_waiting_stages(env):
    task = upload(env).json()["task_id"]
    files = [r["file_id"] for r in file_rows(task)]
    update_file(
        task, files[0], status="running", stage="routing", stage_status="running"
    )
    interrupt_tasks()
    detail = env.service.detail(task)
    assert detail["task"]["status"] == "failed"
    assert detail["files"][0]["stages"][1]["status"] == "failed"
    assert detail["files"][0]["stages"][2]["status"] == "not_run"


@pytest.mark.asyncio
@pytest.mark.parametrize("match_status", ["not_matched", "uncertain"])
async def test_routed_files_have_reason_without_findings_or_audit_calls(
    env, match_status
):
    class ExcludedRoute:
        async def ainvoke(self, messages, **kwargs):
            return AIMessage(
                content=json.dumps(
                    {"match_status": match_status, "match_reason": "未确认同一采购对象"}
                )
            )

    task = upload(env).json()["task_id"]
    # 不提供可调用的审核模型：误入审核分支就会使测试失败。
    models = SimpleNamespace(
        bid_summary=ExcludedRoute(),
        bid_chat=object(),
        evidence_chat=object(),
        embedding=env.embedding,
        rerank=object(),
    )
    await run_audit_batch(task, models=models)
    detail = env.service.detail(task)
    assert detail["task"]["status"] == "completed"
    for file in detail["files"]:
        assert file["route"]["match_status"] == match_status
        assert [s["status"] for s in file["stages"]] == [
            "completed",
            "completed",
            "skipped",
            "skipped",
            "completed",
        ]
        report = env.service.report(task, file["file_id"])
        assert report["counts"] is None
        assert report["result"]["findings"] == []
        assert report["result"]["exclusion_reason"]
        assert report["result"]["report_format_version"] == 2
        assert "审核明细" not in report["document"]["display_markdown"]
        title = report["result"]["report_title"]
        assert title.endswith("项目适用性说明")
        assert (
            env.service.output(task, "md", file["file_id"])
            .read_text()
            .startswith(f"# {title}\n")
        )
        assert (
            env.service.progress(task, True, file["file_id"])["activity"]["model_calls"]
            == 1
        )


@pytest.mark.asyncio
async def test_v2_markdown_failure_retains_json_without_exposing_draft(
    env, monkeypatch
):
    from app.core.errors import BusinessError

    task = upload(env).json()["task_id"]

    def fail(*args):
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr("app.services.audit_batch_runner.write_formal_markdown", fail)
    models = SimpleNamespace(
        bid_summary=RouteModel(),
        bid_chat=ReportModel(env.rules["project-1"]),
        embedding=env.embedding,
        rerank=object(),
        evidence_chat=object(),
    )
    await run_audit_batch(task, models=models)
    detail = env.service.detail(task)
    assert detail["task"]["status"] == "completed_with_report_error"
    for file in detail["files"]:
        assert file["status"] == "completed" and file["word_status"] == "unavailable"
        assert file["outputs"]["md_url"] is None
        assert env.service.output(task, "json", file["file_id"]).is_file()
        assert (
            env.service.report(task, file["file_id"])["document"]["display_markdown"]
            is None
        )
        with pytest.raises(BusinessError) as error:
            env.service.output(task, "md", file["file_id"])
        assert error.value.code == "markdown_export_failed"


@pytest.mark.asyncio
async def test_v1_accepted_snapshot_keeps_legacy_parser_and_output(env):
    task = upload(env).json()["task_id"]
    path = env.service.snapshot_path(task)
    frozen = env.service.snapshot(task)
    frozen.pop("report_format_version")
    frozen.pop("task_name")
    frozen["report_template"] = "历史报告模板，不使用重点问题结构。"
    path.write_text(json.dumps(frozen))

    class LegacyModel(ReportModel):
        async def ainvoke(self, messages, **kwargs):
            assert "历史报告模板" in messages[0].content
            if any(m.type == "tool" for m in messages):
                return AIMessage(content="已完成")
            return tool_response(
                "report_document",
                {
                    "action": "write",
                    "content": filled_report(self.rules),
                    "edits": None,
                },
            )

    models = SimpleNamespace(
        bid_summary=RouteModel(),
        bid_chat=LegacyModel(env.rules["project-1"]),
        embedding=env.embedding,
        rerank=object(),
        evidence_chat=object(),
    )
    await run_audit_batch(task, models=models)
    assert env.service.detail(task)["task"]["status"] == "completed"
    for row in file_rows(task):
        report = env.service.report(task, row["file_id"])
        assert report["result"]["report_format_version"] == 1
        assert report["document"]["display_markdown"] is None
        assert env.service.output(task, "md", row["file_id"]).parent.name == "drafts"
        assert "### 分类总结" in report["document"]["markdown"]


@pytest.mark.asyncio
async def test_search_ledger_finding_formal_quote_end_to_end(env):
    from docx import Document

    from app.llm.rerank_model import RerankResult

    task = upload(env, content="技术响应：实测精度100，保持原始单位。".encode()).json()[
        "task_id"
    ]

    class Rerank:
        async def rerank(self, query, documents, top_n):
            return [
                RerankResult(document_id=d.document_id, score=1.0)
                for d in documents[:top_n]
            ]

    class ReadingModel(ReportModel):
        async def ainvoke(self, messages, **kwargs):
            if any(m.type == "tool" and m.name == "report_document" for m in messages):
                return AIMessage(content="已完成")
            searches = [
                m for m in messages if m.type == "tool" and m.name == "search_documents"
            ]
            if not searches:
                return tool_response(
                    "search_documents",
                    {"querylist": [{"queries": ["精度"], "file_ids": None}]},
                )
            key = re.search(r"^片段：(.+)$", searches[-1].content, re.MULTILINE)[1]
            return tool_response(
                "report_document",
                {
                    "action": "write",
                    "content": filled_report(self.rules, version=2)
                    + f"\n投标片段：{key}\n",
                    "edits": None,
                },
                "save",
            )

    models = SimpleNamespace(
        bid_summary=RouteModel(),
        bid_chat=ReadingModel(env.rules["project-1"]),
        embedding=env.embedding,
        rerank=Rerank(),
        evidence_chat=object(),
    )
    await run_audit_batch(task, models=models)
    detail = env.service.detail(task)
    assert detail["task"]["status"] == "completed", detail
    for file in detail["files"]:
        report = env.service.report(task, file["file_id"])
        finding = report["result"]["findings"][0]
        assert len(finding["bid_chunk_ids"]) == 1
        source = report["result"]["sources"][finding["bid_chunk_ids"][0]]
        assert source["file_id"] == file["file_id"] and source["parse_version"]
        quote = source["quote"]
        assert "实测精度100" in quote
        assert quote in report["document"]["display_markdown"]
        assert finding["bid_chunk_ids"][0] not in report["document"]["display_markdown"]
        assert len(report["source_links"]) == 1
        assert (
            env.client.get(report["source_links"][0]["preview_url"]).status_code == 200
        )
        word = Document(env.service.output(task, "word", file["file_id"]))
        assert quote in "\n".join(p.text for p in word.paragraphs)


def test_trace_redacts_typed_reasoning_without_mutating_model_response():
    from app.audit.local_trace import safe_value

    content = {
        "content": [
            {"type": "text", "text": "已保存报告"},
            {"type": "reasoning", "text": "隐藏思考"},
            {"type": "thinking", "thinking": "隐藏思考"},
        ]
    }
    safe, _ = safe_value(content)
    assert safe == {"content": [{"type": "text", "text": "已保存报告"}]}
    assert len(content["content"]) == 3


@pytest.mark.asyncio
async def test_concurrent_children_keep_file_session_and_parent_scope(env):
    task = upload(env).json()["task_id"]
    files = [r["file_id"] for r in file_rows(task)]

    class DelegatingModel(ReportModel):
        async def ainvoke(self, messages, **kwargs):
            if any(m.type == "tool" and m.name == "report_document" for m in messages):
                return AIMessage(content="处理完毕")
            if any(
                m.type == "tool" and m.name == "request_evidence_review"
                for m in messages
            ):
                return tool_response(
                    "report_document",
                    {
                        "action": "write",
                        "content": filled_report(self.rules, version=2),
                        "edits": None,
                    },
                    "save",
                )
            index = 0 if files[0] in messages[1].content else 1
            return tool_response(
                "request_evidence_review",
                {
                    "checks": [
                        {
                            "rule_id": "rule-1",
                            "verification_request": "核实精度100的证明依据",
                            "file_ids": [env.sources[index][1]],
                        }
                    ]
                },
            )

    class ChildModel:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages, **kwargs):
            return AIMessage(
                content="资料摘要记载精度100，未读取原文，不能进一步确认。"
            )

    await run_audit_batch(
        task,
        models=SimpleNamespace(
            bid_summary=RouteModel(),
            bid_chat=DelegatingModel(env.rules["project-1"]),
            evidence_chat=ChildModel(),
            embedding=env.embedding,
            rerank=object(),
        ),
    )
    assert env.service.detail(task)["task"]["status"] == "completed"
    sessions = set()
    for index, file_id in enumerate(files):
        progress = env.service.progress(task, True, file_id, limit=200)
        assert not progress["has_more"]
        nodes = {u["span"]["span_id"]: u["span"] for u in progress["trace_updates"]}
        checks = [n for n in nodes.values() if n["kind"] == "evidence_check"]
        assert len(checks) == progress["activity"]["evidence_checks"] == 1
        check = checks[0]
        assert check["target_file_ids"] == [env.sources[index][1]]
        assert check["rule_id"] == "rule-1"
        parent = nodes[check["parent_span_id"]]
        assert parent["tool_name"] == "request_evidence_review"
        for node in nodes.values():
            assert node["file_id"] == file_id
            assert node["parent_span_id"] is None or node["parent_span_id"] in nodes
        per_file = {n["session_id"] for n in nodes.values() if n.get("session_id")}
        assert not per_file & sessions
        sessions.update(per_file)


def test_migration_preserves_old_bindings_and_files(tmp_path):
    from app.storage.schema import TABLE_DDLS

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        for ddl in TABLE_DDLS[:6]:
            if "CREATE TABLE IF NOT EXISTS audit_tasks (" in ddl:
                ddl = ddl.replace(
                    "project_id TEXT,", "project_id TEXT NOT NULL,"
                ).replace("bidder_id TEXT,", "bidder_id TEXT NOT NULL,")
            if "CREATE TABLE IF NOT EXISTS files (" in ddl:
                ddl = ddl.replace(
                    "FOREIGN KEY (task_id) REFERENCES audit_tasks(task_id)",
                    "FOREIGN KEY (project_id,bidder_id,task_id) REFERENCES audit_tasks(project_id,bidder_id,task_id)",
                )
            db.execute(ddl)
        db.execute(
            "INSERT INTO projects VALUES ('project-1','rule_ready',NULL,NULL,NULL,'x','x')"
        )
        db.execute(
            "INSERT INTO bidders VALUES ('bidder-1','evidence_ready',NULL,NULL,'x','x')"
        )
        db.execute(
            "INSERT INTO audit_tasks(task_id,project_id,bidder_id,status,created_at,updated_at) VALUES ('audit-1','project-1','bidder-1','completed','x','x')"
        )
        db.execute(
            "INSERT INTO files VALUES ('file-1','project-1','bidder-1','audit-1','bid_document','a.md','a.md','old/a.md','.md',1,'hash','parsed',NULL,NULL,'x','x')"
        )
        db.execute(
            "INSERT INTO audit_events(task_id,actor,event_type,title,created_at) VALUES ('audit-1','agent','done','done','x')"
        )
    initialize_database(path)
    initialize_database(path)
    with open_db(path) as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("SELECT storage_path FROM files").fetchone()[0] == "old/a.md"
        assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 1
        db.execute(
            "INSERT INTO audit_tasks(task_id,status,created_at,updated_at) VALUES ('audit-2','running','x','x')"
        )
