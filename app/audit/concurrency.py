"""同一事件循环共享模型请求名额；不是文件调度锁。"""

import asyncio
from weakref import WeakKeyDictionary

_limiters = WeakKeyDictionary()


def model_limiter(limit):
    loop = asyncio.get_running_loop()
    return _limiters.setdefault(loop, {}).setdefault(limit, asyncio.Semaphore(limit))
