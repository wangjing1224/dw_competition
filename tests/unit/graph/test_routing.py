"""审核完成条件测试。"""

from app.graph.routing import can_build_audit_result


def test_empty_task_can_build_only_after_cursor_reaches_end():
    state = {
        "current_bid_index": 0,
        "bid_files": [],
        "file_results": [],
    }
    assert can_build_audit_result(state)
