"""单进程内异步后台任务提交、引用保持和异常日志收口。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any


logger = logging.getLogger(__name__)


class BackgroundRunner:
    """保持 Task 强引用，避免 asyncio 后台协程被提前回收。"""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def submit(
        self,
        function: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(function(*args, **kwargs))
        self._tasks.add(task)
        task.add_done_callback(self._finish)
        return task

    def _finish(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.exception("后台审核任务异常", exc_info=exception)

    async def wait(self) -> None:
        """测试和应用优雅关闭时等待当前进程内任务结束。"""

        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)


background_runner = BackgroundRunner()
