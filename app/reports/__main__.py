"""uv run python -m app.reports audit-N：从草稿和快照重导出，无模型调用。"""

import argparse

from app.reports.service import ReportService


def main():
    parser = argparse.ArgumentParser(
        description="从保存的 Markdown/来源快照重新校验并生成 JSON/Word"
    )
    parser.add_argument("task_id")
    arguments = parser.parse_args()
    service = ReportService()
    paths = service.rebuild_from_markdown(arguments.task_id)
    task = service.task_repository.require(arguments.task_id)
    print(f"task_id={task.task_id} status={task.status} reports={len(paths)}")
    if task.error_message:
        print(task.error_message)


if __name__ == "__main__":
    main()
