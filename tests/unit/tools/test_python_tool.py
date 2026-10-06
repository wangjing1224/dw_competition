"""执行真实的短程序；不启动业务服务、不请求模型、不使用Docker。"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.core.config import PythonToolSettings
from app.retrieval.models import IndexFilter
from app.tools import common_tools
from app.tools.common_tools import CommonTool, PythonSandbox, build_python_tool


@pytest.mark.parametrize("with_search", [False, True])
def test_common_provider_includes_python_once(temp_settings, with_search):
    provider = CommonTool(
        parsed_documents={},
        settings=temp_settings,
        retrieval=object() if with_search else None,
        query_filter=IndexFilter(
            scope_id="rules-project-1", data_role="rule_source", project_id="project-1",
        ) if with_search else None,
    )
    expected = ["view_image", "run_python"]
    if with_search:
        expected.insert(0, "search_documents")
        assert provider.search_tool().name == "search_documents"
    for _ in range(2):
        assert [tool.name for tool in provider.tools()] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [
    ("print(17 * 23)", "391\n"),
    ("from decimal import Decimal\nprint(Decimal('0.1') + Decimal('0.2'))", "0.3\n"),
    ("from datetime import datetime\na = datetime.strptime('2026-01-02', '%Y-%m-%d')\nprint(a.strftime('%Y-%m-%d'))", "2026-01-02\n"),
    ("import json, re\na=json.loads('[3, 1, 3]')\nprint(sorted(set(a)))\nprint(re.findall(r'\\d+', '甲12乙3'))", "[1, 3]\n['12', '3']\n"),
    ("def twice(x):\n    return x * 2\nprint([twice(x) for x in range(3)])", "[0, 2, 4]\n"),
    ("value=3", ""),
])
async def test_real_execution(code, expected):
    assert await PythonSandbox(PythonToolSettings()).execute(code) == {
        "status": "success", "result": expected,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [
    "import os", "import socket", "import subprocess", "import pathlib",
    "from json import load", "from re import os", "import time", "import _strptime",
    "open('/tmp/file')", "eval('2+2')", "exec('print(1)')", "globals()",
    "getattr(1, '__class__')", "print((1).__class__)",
    "print((x for x in [1]).gi_frame.f_globals)",
    "print('{0.__class__}'.format(1))", "import math as __builtins__",
])
async def test_permission_checks_before_start(monkeypatch, code):
    execute = AsyncMock()
    monkeypatch.setattr(common_tools, "_execute_process", execute)
    result = await PythonSandbox(PythonToolSettings()).execute("print('before')\n" + code)
    assert result["status"] == "error" and "PermissionError" in result["result"]
    assert "before" not in result["result"]
    execute.assert_not_called()


@pytest.mark.asyncio
async def test_syntax_runtime_output_and_timeout_errors():
    sandbox = PythonSandbox(PythonToolSettings(timeout_seconds=0.5, max_output_bytes=256))
    for code, error in [
        ("", "ValueError"), ("print(", "SyntaxError"), ("print(1/0)", "ZeroDivisionError"),
        ("print('x' * 10000)", "OutputLimitError"), ("while True: pass", "TimeoutError"),
    ]:
        result = await sandbox.execute(code)
        assert result["status"] == "error" and error in result["result"]
        assert set(result) == {"status", "result"}
    assert (await sandbox.execute("print(2)"))["status"] == "success"
    partial = await sandbox.execute("print('partial output')\nprint(1/0)")
    assert partial["status"] == "error"
    assert "ZeroDivisionError" in partial["result"] and "第2行" in partial["result"]
    assert "partial output" not in partial["result"]


@pytest.mark.asyncio
async def test_code_budget_disabled_and_no_cross_call_state():
    assert "字节" in (await PythonSandbox(PythonToolSettings(max_code_bytes=5)).execute("print(1)"))["result"]
    assert "已禁用" in (await PythonSandbox(PythonToolSettings(enabled=False)).execute("print(1)"))["result"]
    sandbox = PythonSandbox(PythonToolSettings())
    await sandbox.execute("a = 123")
    assert "NameError" in (await sandbox.execute("print(a)"))["result"]


@pytest.mark.asyncio
async def test_process_environment_workdir_and_cancel_cleanup(monkeypatch):
    spawn = asyncio.create_subprocess_exec
    started = asyncio.Event()
    processes, directories = [], []

    async def capture(*args, **kwargs):
        assert args[:5] == (sys.executable, "-I", "-S", "-B", "-u")
        assert set(kwargs["env"]) == {"LANG"}
        assert kwargs["start_new_session"] is True
        assert not os.listdir(kwargs["cwd"])
        directories.append(kwargs["cwd"])
        process = await spawn(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    task = asyncio.create_task(PythonSandbox(PythonToolSettings()).execute("while True: pass"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(p.returncode is not None for p in processes)
    assert all(not os.path.exists(path) for path in directories)
    assert common_tools._active == 0


@pytest.mark.asyncio
async def test_four_factories_share_process_slots(monkeypatch):
    active, peak = 0, 0

    async def execute(*args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return 0, "ok", ""

    monkeypatch.setattr(common_tools, "_execute_process", execute)
    sandboxes = [PythonSandbox(PythonToolSettings(max_concurrency=2)) for _ in range(4)]
    await asyncio.gather(*(sandbox.execute("print(1)") for sandbox in sandboxes))
    assert peak == 2 and common_tools._active == 0


@pytest.mark.asyncio
async def test_single_model_input_and_error_toolmessage():
    tool = build_python_tool(PythonToolSettings())
    schema = convert_to_openai_tool(tool)["function"]["parameters"]
    assert set(schema["properties"]) == {"code"}
    assert schema["required"] == ["code"]
    for code, status in [("print(3)", "success"), ("import os", "error")]:
        message = await tool.ainvoke({"name": tool.name, "args": {"code": code}, "id": "c", "type": "tool_call"})
        assert message.status == status and message.tool_call_id == "c"
        result = json.loads(message.content)
        assert set(result) == {"status", "result"} and result["status"] == status


@pytest.mark.asyncio
async def test_error_can_be_corrected_in_existing_controller(temp_settings):
    from tests.helpers.audit import SequenceModel
    from tests.unit.audit.test_audit_budget import run_tool_test
    from app.llm.input_budget import InputContext, validate_tool_pairs

    tool = build_python_tool(temp_settings.python_tool)
    model = SequenceModel([
        AIMessage(content="", tool_calls=[{"name": tool.name, "id": "bad", "args": {"code": "print(1/0)"}}]),
        AIMessage(content="", tool_calls=[{"name": tool.name, "id": "good", "args": {"code": "print(4/2)"}}]),
        "采用执行结果2.0。",
    ])
    await run_tool_test(temp_settings, model, [tool])
    messages = model.calls[-1][0]
    validate_tool_pairs(InputContext(tuple(messages)).wire_messages())
    responses = [m for m in messages if m.type == "tool"]
    assert [m.status for m in responses] == ["error", "success"]
    assert json.loads(responses[1].content)["result"] == "2.0\n"


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS硬内存上限仅Linux支持")
@pytest.mark.asyncio
async def test_linux_memory_limit():
    result = await PythonSandbox(PythonToolSettings(memory_mb=64)).execute("a = 'x' * (200 * 1024 * 1024)")
    assert result["status"] == "error" and "MemoryError" in result["result"]
