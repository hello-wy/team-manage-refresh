"""同一进程内按上游工作区串行检查和占用席位，支持同一任务嵌套调用。"""
import asyncio
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary


class _AccountLock:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.owner = None


_locks = WeakValueDictionary()


@asynccontextmanager
async def seat_account_lock(account_id):
    entry = _locks.get(account_id)
    if entry is None:
        entry = _AccountLock()
        _locks[account_id] = entry
    task = asyncio.current_task()
    if entry.owner is task:
        yield
        return
    async with entry.lock:
        entry.owner = task
        try:
            yield
        finally:
            entry.owner = None
